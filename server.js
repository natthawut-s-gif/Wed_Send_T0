require("dotenv").config();

const fs = require("fs/promises");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { promisify } = require("util");
const { execFile, spawn } = require("child_process");
const express = require("express");
const multer = require("multer");
const axios = require("axios");
const FormData = require("form-data");
const packageJson = require("./package.json");

const app = express();
const execFileAsync = promisify(execFile);

const host = process.env.HOST || "0.0.0.0";
const port = Number(process.env.PORT || 3000);
const dataDir = process.env.DATA_DIR
  ? path.resolve(process.env.DATA_DIR)
  : __dirname;
const settingsFile = process.env.WEBHOOK_SETTINGS_FILE
  ? path.resolve(process.env.WEBHOOK_SETTINGS_FILE)
  : path.join(dataDir, "webhook-settings.json");
const uploadHistoryFile = process.env.UPLOAD_HISTORY_FILE
  ? path.resolve(process.env.UPLOAD_HISTORY_FILE)
  : path.join(dataDir, "upload-history.json");
const defaultWebhookUrl = process.env.N8N_WEBHOOK_URL || "";
const defaultExportDocWebhookUrl = process.env.EXPORT_DOC_WEBHOOK_URL || "";
const defaultCommandWebhookUrl = process.env.COMMAND_WEBHOOK_URL || "";
const defaultLoginWebhookUrl = process.env.LOGIN_WEBHOOK_URL || "";
const defaultGoogleClientId = process.env.GOOGLE_CLIENT_ID || "";
const defaultMicrosoftClientId = process.env.MICROSOFT_CLIENT_ID || "";
const defaultMicrosoftAuthority =
  process.env.MICROSOFT_AUTHORITY || "https://login.microsoftonline.com/common";
const loginPasswordSecret = process.env.LOGIN_PASSWORD_SECRET || "";
const uploadHistoryLimit = Number(process.env.UPLOAD_HISTORY_LIMIT || 100);
const maxFiles = Number(process.env.MAX_FILES || 10);
const maxFileSizeMb = Number(process.env.MAX_FILE_SIZE_MB || 10);
const maxFileSizeBytes = maxFileSizeMb * 1024 * 1024;
const maxTotalUploadMb = Number(process.env.MAX_TOTAL_UPLOAD_MB || 15);
const maxTotalUploadBytes = maxTotalUploadMb * 1024 * 1024;
const appStartedAt = new Date();
const pythonCommand = process.env.PYTHON_COMMAND || "python";
const appVersion = packageJson.version || "0.0.0";
let currentWebhookUrl = defaultWebhookUrl;
let currentExportDocWebhookUrl = defaultExportDocWebhookUrl;
let currentCommandWebhookUrl = defaultCommandWebhookUrl;
let currentLoginWebhookUrl = defaultLoginWebhookUrl;
let currentGoogleClientId = defaultGoogleClientId;
let currentMicrosoftClientId = defaultMicrosoftClientId;
let currentMicrosoftAuthority = defaultMicrosoftAuthority;
let uploadHistory = [];
const uploadProgressStore = new Map();

const runtimeStats = {
  uploadAttempts: 0,
  successfulForwards: 0,
  failedForwards: 0,
  validationFailures: 0,
  batchAttempts: 0,
  successfulBatches: 0,
  failedBatches: 0,
  lastUploadAt: null,
  lastSuccessAt: null,
  lastFailureAt: null,
  lastWebhookStatus: null,
  lastError: null
};

const allowedMimeTypes = new Set([
  "application/pdf",
  "image/png",
  "image/jpeg",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "text/csv",
  "application/csv",
  "text/plain"
]);

const allowedExtensions = new Set([
  ".pdf",
  ".png",
  ".jpg",
  ".jpeg",
  ".doc",
  ".docx",
  ".xls",
  ".xlsx",
  ".csv"
]);

const storage = multer.memoryStorage();

function getRuntimeMetrics() {
  return {
    version: appVersion,
    startedAt: appStartedAt.toISOString(),
    uptimeSeconds: Math.floor((Date.now() - appStartedAt.getTime()) / 1000),
    pid: process.pid,
    stats: runtimeStats
  };
}

function getAppMeta() {
  return {
    appName: "DocExtraction",
    appVersion
  };
}

function getListenUrls() {
  const urls = [`http://localhost:${port}`];
  const networkInterfaces = os.networkInterfaces();

  for (const interfaceEntries of Object.values(networkInterfaces)) {
    for (const entry of interfaceEntries || []) {
      if (!entry || entry.internal) {
        continue;
      }

      const family = typeof entry.family === "string" ? entry.family : String(entry.family);
      if (family !== "IPv4") {
        continue;
      }

      urls.push(`http://${entry.address}:${port}`);
    }
  }

  return [...new Set(urls)];
}

function getWebhookSettings() {
  return {
    webhookUrl: currentWebhookUrl,
    actionWebhookUrl: currentExportDocWebhookUrl,
    commandWebhookUrl: currentCommandWebhookUrl,
    loginWebhookUrl: currentLoginWebhookUrl,
    googleClientId: currentGoogleClientId,
    microsoftClientId: currentMicrosoftClientId,
    microsoftAuthority: currentMicrosoftAuthority
  };
}

function getAuthSettings() {
  return {
    loginWebhookConfigured: Boolean(currentLoginWebhookUrl),
    googleLoginConfigured: Boolean(currentGoogleClientId),
    microsoftLoginConfigured: Boolean(currentMicrosoftClientId),
    loginPasswordEncryptionConfigured: Boolean(loginPasswordSecret)
  };
}

async function ensureParentDirectory(filePath) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
}

function setUploadProgress(uploadRequestId, patch) {
  if (!uploadRequestId) {
    return;
  }

  const current = uploadProgressStore.get(uploadRequestId) || {
    uploadRequestId,
    phase: "queued",
    pageCount: 0,
    preparedPages: 0,
    sentPages: 0,
    currentPageNumber: null,
    sourceOriginalName: "",
    outboundFileCount: 0,
    fileCount: 0,
    error: ""
  };

  uploadProgressStore.set(uploadRequestId, {
    ...current,
    ...patch,
    updatedAt: new Date().toISOString()
  });
}

function finalizeUploadProgress(uploadRequestId, patch) {
  if (!uploadRequestId) {
    return;
  }

  setUploadProgress(uploadRequestId, patch);

  const cleanupTimer = setTimeout(() => {
    uploadProgressStore.delete(uploadRequestId);
  }, 10 * 60 * 1000);

  if (typeof cleanupTimer.unref === "function") {
    cleanupTimer.unref();
  }
}

function sanitizeUploadHistory(payload) {
  if (!Array.isArray(payload)) {
    return [];
  }

  return payload
    .map((item) => {
      if (typeof item === "string") {
        const name = item.trim();
        return name
          ? {
              name,
              email: "",
              createdAt: null
            }
          : null;
      }

      if (!item || typeof item !== "object") {
        return null;
      }

      const name = typeof item.name === "string" ? item.name.trim() : "";
      const email = typeof item.email === "string" ? item.email.trim().toLowerCase() : "";
      const createdAt = typeof item.createdAt === "string" ? item.createdAt.trim() : null;

      if (!name) {
        return null;
      }

      return {
        name,
        email,
        createdAt: createdAt || null
      };
    })
    .filter(Boolean)
    .slice(0, uploadHistoryLimit);
}

function sanitizeWebhookSettings(payload) {
  const webhookUrl = typeof payload?.webhookUrl === "string" ? payload.webhookUrl.trim() : "";
  const actionWebhookUrl =
    typeof payload?.actionWebhookUrl === "string" ? payload.actionWebhookUrl.trim() : "";
  const commandWebhookUrl =
    typeof payload?.commandWebhookUrl === "string" ? payload.commandWebhookUrl.trim() : "";
  const loginWebhookUrl =
    typeof payload?.loginWebhookUrl === "string" ? payload.loginWebhookUrl.trim() : "";
  const googleClientId =
    typeof payload?.googleClientId === "string" ? payload.googleClientId.trim() : "";
  const microsoftClientId =
    typeof payload?.microsoftClientId === "string" ? payload.microsoftClientId.trim() : "";
  const microsoftAuthority =
    typeof payload?.microsoftAuthority === "string"
      ? payload.microsoftAuthority.trim()
      : defaultMicrosoftAuthority;

  for (const [label, value] of [
    ["Webhook URL", webhookUrl],
    ["Action Webhook URL", actionWebhookUrl],
    ["Command Webhook URL", commandWebhookUrl]
  ]) {
    if (!value) {
      throw new Error(`${label} is required.`);
    }

    let parsedUrl;
    try {
      parsedUrl = new URL(value);
    } catch (error) {
      throw new Error(`${label} must be a valid URL.`);
    }

    if (!["http:", "https:"].includes(parsedUrl.protocol)) {
      throw new Error(`${label} must start with http:// or https://`);
    }
  }

  if (loginWebhookUrl) {
    let parsedLoginWebhookUrl;
    try {
      parsedLoginWebhookUrl = new URL(loginWebhookUrl);
    } catch (error) {
      throw new Error("Login Webhook URL must be a valid URL.");
    }

    if (!["http:", "https:"].includes(parsedLoginWebhookUrl.protocol)) {
      throw new Error("Login Webhook URL must start with http:// or https://");
    }
  }

  if (microsoftAuthority) {
    let parsedMicrosoftAuthority;
    try {
      parsedMicrosoftAuthority = new URL(microsoftAuthority);
    } catch (error) {
      throw new Error("Microsoft Authority must be a valid URL.");
    }

    if (!["http:", "https:"].includes(parsedMicrosoftAuthority.protocol)) {
      throw new Error("Microsoft Authority must start with http:// or https://");
    }
  }

  return {
    webhookUrl,
    actionWebhookUrl,
    commandWebhookUrl,
    loginWebhookUrl,
    googleClientId,
    microsoftClientId,
    microsoftAuthority: microsoftAuthority || defaultMicrosoftAuthority
  };
}

async function loadWebhookSettings() {
  try {
    const raw = await fs.readFile(settingsFile, "utf8");
    const parsed = sanitizeWebhookSettings(JSON.parse(raw));
    currentWebhookUrl = parsed.webhookUrl;
    currentExportDocWebhookUrl = parsed.actionWebhookUrl;
    currentCommandWebhookUrl = parsed.commandWebhookUrl;
    currentLoginWebhookUrl = parsed.loginWebhookUrl;
    currentGoogleClientId = parsed.googleClientId;
    currentMicrosoftClientId = parsed.microsoftClientId;
    currentMicrosoftAuthority = parsed.microsoftAuthority;
  } catch (error) {
    if (error.code === "ENOENT") {
      await saveWebhookSettings({
        webhookUrl: currentWebhookUrl,
        actionWebhookUrl: currentExportDocWebhookUrl,
        commandWebhookUrl: currentCommandWebhookUrl,
        loginWebhookUrl: currentLoginWebhookUrl,
        googleClientId: currentGoogleClientId,
        microsoftClientId: currentMicrosoftClientId,
        microsoftAuthority: currentMicrosoftAuthority
      });
      return;
    }

    console.warn(`[settings] failed to load webhook settings, using current defaults: ${error.message}`);
  }
}

async function saveWebhookSettings(payload) {
  const sanitized = sanitizeWebhookSettings(payload);
  await ensureParentDirectory(settingsFile);
  await fs.writeFile(settingsFile, `${JSON.stringify(sanitized, null, 2)}\n`, "utf8");
  currentWebhookUrl = sanitized.webhookUrl;
  currentExportDocWebhookUrl = sanitized.actionWebhookUrl;
  currentCommandWebhookUrl = sanitized.commandWebhookUrl;
  currentLoginWebhookUrl = sanitized.loginWebhookUrl;
  currentGoogleClientId = sanitized.googleClientId;
  currentMicrosoftClientId = sanitized.microsoftClientId;
  currentMicrosoftAuthority = sanitized.microsoftAuthority;
  return sanitized;
}

function encryptPasswordForWebhook(password) {
  if (!loginPasswordSecret) {
    throw new Error("LOGIN_PASSWORD_SECRET is not configured.");
  }

  const key = crypto.createHash("sha256").update(loginPasswordSecret).digest();
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([
    cipher.update(String(password || ""), "utf8"),
    cipher.final()
  ]);
  const authTag = cipher.getAuthTag();

  return Buffer.from(
    JSON.stringify({
      algorithm: "aes-256-gcm",
      iv: iv.toString("base64"),
      authTag: authTag.toString("base64"),
      ciphertext: encrypted.toString("base64")
    }),
    "utf8"
  ).toString("base64");
}

function decryptPasswordFromWebhook(encryptedPayload) {
  if (!loginPasswordSecret) {
    throw new Error("LOGIN_PASSWORD_SECRET is not configured.");
  }

  if (!encryptedPayload) {
    throw new Error("Encrypted password payload is empty.");
  }

  const decoded = JSON.parse(Buffer.from(String(encryptedPayload), "base64").toString("utf8"));
  const key = crypto.createHash("sha256").update(loginPasswordSecret).digest();
  const decipher = crypto.createDecipheriv(
    "aes-256-gcm",
    key,
    Buffer.from(decoded.iv, "base64")
  );
  decipher.setAuthTag(Buffer.from(decoded.authTag, "base64"));
  const decrypted = Buffer.concat([
    decipher.update(Buffer.from(decoded.ciphertext, "base64")),
    decipher.final()
  ]);
  return decrypted.toString("utf8");
}

function extractUserRecordFromWebhookBody(webhookBody) {
  if (Array.isArray(webhookBody)) {
    return webhookBody.find((item) => item && typeof item === "object") || null;
  }

  if (webhookBody && typeof webhookBody === "object") {
    if (Array.isArray(webhookBody.data)) {
      return webhookBody.data.find((item) => item && typeof item === "object") || null;
    }

    if (webhookBody.user && typeof webhookBody.user === "object") {
      return webhookBody.user;
    }

    return webhookBody;
  }

  return null;
}

function comparePasswords(left, right) {
  const leftBuffer = Buffer.from(String(left || ""), "utf8");
  const rightBuffer = Buffer.from(String(right || ""), "utf8");

  if (leftBuffer.length !== rightBuffer.length) {
    return false;
  }

  return crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

async function loadUploadHistory() {
  try {
    const raw = await fs.readFile(uploadHistoryFile, "utf8");
    uploadHistory = sanitizeUploadHistory(JSON.parse(raw));
  } catch (error) {
    if (error.code === "ENOENT") {
      uploadHistory = [];
      await saveUploadHistory(uploadHistory);
      return;
    }

    uploadHistory = [];
    console.warn(`[history] failed to load upload history: ${error.message}`);
  }
}

async function saveUploadHistory(history) {
  const sanitized = sanitizeUploadHistory(history);
  uploadHistory = sanitized;
  await ensureParentDirectory(uploadHistoryFile);
  await fs.writeFile(uploadHistoryFile, `${JSON.stringify(sanitized, null, 2)}\n`, "utf8");
  return uploadHistory;
}

async function appendUploadHistory(names, email = "") {
  const normalizedEmail = typeof email === "string" ? email.trim().toLowerCase() : "";
  const entries = (Array.isArray(names) ? names : [])
    .map((name) => (typeof name === "string" ? name.trim() : ""))
    .filter(Boolean)
    .map((name) => ({
      name,
      email: normalizedEmail,
      createdAt: new Date().toISOString()
    }));
  const sanitizedEntries = sanitizeUploadHistory(entries);
  if (!sanitizedEntries.length) {
    return uploadHistory;
  }

  const nextHistory = [...sanitizedEntries.reverse(), ...uploadHistory].slice(0, uploadHistoryLimit);
  return saveUploadHistory(nextHistory);
}

function asciiSafeStem(value) {
  const normalized = (value || "")
    .normalize("NFKD")
    .replace(/[^\x00-\x7F]/g, "");
  const cleaned = normalized.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[_\-.]+|[_\-.]+$/g, "");
  return cleaned || "uploaded_file";
}

function repairMultipartFilename(value) {
  if (!value) {
    return value;
  }

  try {
    const repaired = Buffer.from(value, "latin1").toString("utf8");
    const repairedHasUsefulUnicode = /[\u0E00-\u0E7F]/.test(repaired) || /[^\x00-\x7F]/.test(repaired);
    const originalLooksBroken = /[ÃàâÐ]/.test(value);

    if (repairedHasUsefulUnicode || originalLooksBroken) {
      return repaired;
    }
  } catch (error) {
    return value;
  }

  return value;
}

function buildSafeFilename(originalName) {
  const extension = path.extname(originalName || "");
  const stem = path.basename(originalName || "uploaded_file", extension);
  return `${asciiSafeStem(stem)}${extension.toLowerCase()}`;
}

async function preprocessForOutboundFiles(file) {
  const repairedOriginalName = repairMultipartFilename(file.originalname || "");
  const extension = path.extname(repairedOriginalName || "").toLowerCase();
  const uploadRequestId =
    typeof file.uploadRequestId === "string" ? file.uploadRequestId.trim() : "";
  const isPdfSource = extension === ".pdf";
  const isImageSource = [".png", ".jpg", ".jpeg"].includes(extension);

  if (!isPdfSource && !isImageSource) {
    setUploadProgress(uploadRequestId, {
      phase: "forwarding_original",
      sourceOriginalName: repairedOriginalName,
      pageCount: 1,
      preparedPages: 1,
      sentPages: 0,
      currentPageNumber: null,
      outboundFileCount: 1
    });

    return [
      {
        filename: buildSafeFilename(repairedOriginalName),
        mimeType: file.mimetype,
        size: file.size,
        buffer: file.buffer,
        processedKind: "original",
        pageNumber: null,
        pageCount: null,
        sourceOriginalName: repairedOriginalName
      }
    ];
  }

  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ocr-preprocess-"));
  const inputPath = path.join(tempRoot, `input${extension || ".bin"}`);
  const outputDir = path.join(tempRoot, "outputs");

  try {
    await fs.writeFile(inputPath, file.buffer);
    setUploadProgress(uploadRequestId, {
      phase: isPdfSource ? "rendering_pdf" : "preprocessing_image",
      sourceOriginalName: repairedOriginalName,
      pageCount: 0,
      preparedPages: 0,
      sentPages: 0,
      currentPageNumber: null,
      outboundFileCount: 0
    });

    const payload = await new Promise((resolve, reject) => {
      const child = spawn(
        pythonCommand,
        [
          path.join(__dirname, "ocr_preprocess.py"),
          "--input",
          inputPath,
          "--output-dir",
          outputDir,
          "--original-name",
          repairedOriginalName
        ],
        {
          cwd: __dirname
        }
      );

      let stdout = "";
      let stderr = "";
      let stderrBuffer = "";

      child.stdout.on("data", (chunk) => {
        stdout += String(chunk);
      });

      child.stderr.on("data", (chunk) => {
        const text = String(chunk);
        stderr += text;
        stderrBuffer += text;
        const lines = stderrBuffer.split(/\r?\n/);
        stderrBuffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) {
            continue;
          }

          if (trimmed.startsWith("PROGRESS ")) {
            try {
              const progressPayload = JSON.parse(trimmed.slice("PROGRESS ".length));
              setUploadProgress(uploadRequestId, {
                phase: progressPayload.phase || "rendering_pdf",
                pageCount: Number(progressPayload.pageCount || 0),
                preparedPages: Number(progressPayload.pageNumber || 0),
                currentPageNumber: Number(progressPayload.pageNumber || 0),
                sourceOriginalName: repairedOriginalName
              });
            } catch (error) {
              console.warn(`[pdf-render] invalid progress payload: ${trimmed}`);
            }
            continue;
          }

          console.warn(`[pdf-render] ${trimmed}`);
        }
      });

      child.on("error", (error) => {
        reject(error);
      });

      child.on("close", (code) => {
        if (stderrBuffer.trim()) {
          const trailing = stderrBuffer.trim();
          if (!trailing.startsWith("PROGRESS ")) {
            console.warn(`[pdf-render] ${trailing}`);
          }
        }

        if (code !== 0) {
          reject(new Error(stderr || stdout || `Renderer exited with code ${code}`));
          return;
        }

        try {
          resolve(JSON.parse(stdout));
        } catch (error) {
          reject(new Error(stdout || "Renderer returned invalid JSON."));
        }
      });
    });

    const outputs = [];

    for (const item of payload.outputs || []) {
      const renderedBuffer = await fs.readFile(item.path);
      outputs.push({
        filename: item.filename,
        mimeType: item.mimeType,
        size: item.size,
        buffer: renderedBuffer,
        processedKind: item.processedKind,
        pageNumber: item.pageNumber,
        pageCount: item.pageCount,
        sourceOriginalName: repairedOriginalName,
        ocrCandidate: item.ocrCandidate || "",
        ocrConfidence: item.ocrConfidence ?? null,
        ocrScore: item.ocrScore ?? null,
        ocrTextLength: item.ocrTextLength ?? null,
        documentProfile: item.documentProfile || "",
        backgroundSaturation: item.backgroundSaturation ?? null,
        tintStrength: item.tintStrength ?? null
      });
    }

    setUploadProgress(uploadRequestId, {
      phase: isPdfSource ? "forwarding_pdf_pages" : "forwarding_image_ocr",
      sourceOriginalName: repairedOriginalName,
      pageCount: outputs.length,
      preparedPages: outputs.length,
      sentPages: 0,
      currentPageNumber: outputs.length ? 1 : null,
      outboundFileCount: outputs.length
    });

    return outputs;
  } catch (error) {
    const details = error.stderr || error.stdout || error.message;
    throw new Error(`OCR preprocessing failed for ${repairedOriginalName}: ${details}`);
  } finally {
    await fs.rm(tempRoot, { recursive: true, force: true });
  }
}

function fileFilter(req, file, callback) {
  const extension = path.extname(file.originalname || "").toLowerCase();
  const isAllowedExtension = allowedExtensions.has(extension);
  const isAllowedMime =
    allowedMimeTypes.has(file.mimetype) ||
    (extension === ".csv" && file.mimetype === "application/vnd.ms-excel");

  if (!isAllowedExtension || !isAllowedMime) {
    callback(
      new Error(
        `Unsupported file type: ${file.originalname}. Allowed: PDF, PNG, JPG, Word, Excel, CSV.`
      )
    );
    return;
  }

  callback(null, true);
}

const upload = multer({
  storage,
  limits: {
    fileSize: maxFileSizeBytes,
    files: maxFiles
  },
  fileFilter
});

app.use(express.static(path.join(__dirname, "public")));
app.use(express.json());

app.get("/health", (req, res) => {
  res.json({
    ok: true,
    ...getAppMeta(),
    host,
    port,
    urls: getListenUrls(),
    webhookConfigured: Boolean(currentWebhookUrl),
    exportDocWebhookConfigured: Boolean(currentExportDocWebhookUrl),
    commandWebhookConfigured: Boolean(currentCommandWebhookUrl),
    ...getAuthSettings(),
    maxFiles,
    maxFileSizeMb,
    maxTotalUploadMb,
    runtime: getRuntimeMetrics()
  });
});

app.get("/settings/webhooks", (req, res) => {
  res.json({
    ok: true,
    ...getAppMeta(),
    ...getWebhookSettings(),
    webhookConfigured: Boolean(currentWebhookUrl),
    actionWebhookConfigured: Boolean(currentExportDocWebhookUrl),
    commandWebhookConfigured: Boolean(currentCommandWebhookUrl),
    ...getAuthSettings()
  });
});

app.get("/upload-history", (req, res) => {
  res.json({
    ok: true,
    items: uploadHistory
  });
});

app.get("/upload-progress/:uploadRequestId", (req, res) => {
  const uploadRequestId = typeof req.params?.uploadRequestId === "string" ? req.params.uploadRequestId : "";
  const progress = uploadProgressStore.get(uploadRequestId);

  if (!progress) {
    res.status(404).json({
      ok: false,
      message: "Upload progress not found."
    });
    return;
  }

  res.json({
    ok: true,
    progress
  });
});

app.post("/upload-history/clear", async (req, res) => {
  try {
    const email =
      typeof req.body?.email === "string" ? req.body.email.trim().toLowerCase() : "";
    const nextHistory = email
      ? uploadHistory.filter((item) => item.email !== email)
      : [];
    await saveUploadHistory(nextHistory);
    res.json({
      ok: true,
      items: uploadHistory
    });
  } catch (error) {
    res.status(500).json({
      ok: false,
      message: "Failed to clear upload history."
    });
  }
});

app.post("/settings/webhooks", async (req, res) => {
  try {
    const saved = await saveWebhookSettings(req.body || {});
    res.json({
      ok: true,
      ...getAppMeta(),
      message: "Webhook settings updated.",
      ...saved,
      webhookConfigured: Boolean(saved.webhookUrl),
      actionWebhookConfigured: Boolean(saved.actionWebhookUrl),
      commandWebhookConfigured: Boolean(saved.commandWebhookUrl),
      ...getAuthSettings()
    });
  } catch (error) {
    res.status(400).json({
      ok: false,
      message: error.message
    });
  }
});

app.post("/auth/login", async (req, res) => {
  const provider =
    typeof req.body?.provider === "string" ? req.body.provider.trim().toLowerCase() : "";
  const email =
    typeof req.body?.email === "string" ? req.body.email.trim().toLowerCase() : "";
  const password = typeof req.body?.password === "string" ? req.body.password : "";
  const idToken = typeof req.body?.idToken === "string" ? req.body.idToken.trim() : "";
  const accessToken =
    typeof req.body?.accessToken === "string" ? req.body.accessToken.trim() : "";

  if (!currentLoginWebhookUrl) {
    res.status(500).json({
      ok: false,
      message: "LOGIN_WEBHOOK_URL is not configured."
    });
    return;
  }

  if (!["manual", "google", "microsoft"].includes(provider)) {
    res.status(400).json({
      ok: false,
      message: "Unsupported login provider."
    });
    return;
  }

  if (!email) {
    res.status(400).json({
      ok: false,
      message: "Email is required."
    });
    return;
  }

  if (provider === "manual" && !password) {
    res.status(400).json({
      ok: false,
      message: "Password is required for manual login."
    });
    return;
  }

  try {
    const encryptedPassword = provider === "manual" ? encryptPasswordForWebhook(password) : "";
    const webhookResponse = await axios.post(
      currentLoginWebhookUrl,
      {
        node: "login",
        provider,
        email,
        pass: encryptedPassword,
        ...(idToken ? { idToken } : {}),
        ...(accessToken ? { accessToken } : {})
      },
      {
        headers: {
          "Content-Type": "application/json"
        },
        validateStatus: () => true
      }
    );

    if (webhookResponse.status >= 400) {
      res.status(502).json({
        ok: false,
        message: "Login webhook returned an error.",
        provider,
        email,
        webhookStatus: webhookResponse.status,
        webhookBody: webhookResponse.data
      });
      return;
    }

    if (provider === "manual") {
      const userRecord = extractUserRecordFromWebhookBody(webhookResponse.data);

      if (!userRecord || !userRecord.pass) {
        res.status(401).json({
          ok: false,
          message: "User not found or password data is missing.",
          provider,
          email,
          webhookStatus: webhookResponse.status,
          webhookBody: webhookResponse.data
        });
        return;
      }

      const storedPassword = decryptPasswordFromWebhook(userRecord.pass);
      if (!comparePasswords(password, storedPassword)) {
        res.status(401).json({
          ok: false,
          message: "Invalid email or password.",
          provider,
          email,
          webhookStatus: webhookResponse.status,
          webhookBody: webhookResponse.data
        });
        return;
      }
    }

    res.json({
      ok: true,
      message: "Login request sent successfully.",
      provider,
      email,
      webhookStatus: webhookResponse.status,
      webhookBody: webhookResponse.data
    });
  } catch (error) {
    res.status(502).json({
      ok: false,
      message: "Failed to reach login webhook.",
      provider,
      email,
      error: error.message
    });
  }
});

app.post("/auth/register", async (req, res) => {
  const provider =
    typeof req.body?.provider === "string" ? req.body.provider.trim().toLowerCase() : "manual";
  const email =
    typeof req.body?.email === "string" ? req.body.email.trim().toLowerCase() : "";
  const password = typeof req.body?.password === "string" ? req.body.password : "";

  if (!currentLoginWebhookUrl) {
    res.status(500).json({
      ok: false,
      message: "LOGIN_WEBHOOK_URL is not configured."
    });
    return;
  }

  if (provider !== "manual") {
    res.status(400).json({
      ok: false,
      message: "Register currently supports manual provider only."
    });
    return;
  }

  if (!email || !password) {
    res.status(400).json({
      ok: false,
      message: "Email and password are required."
    });
    return;
  }

  try {
    const encryptedPassword = encryptPasswordForWebhook(password);
    const webhookResponse = await axios.post(
      currentLoginWebhookUrl,
      {
        node: "register",
        provider,
        email,
        pass: encryptedPassword
      },
      {
        headers: {
          "Content-Type": "application/json"
        },
        validateStatus: () => true
      }
    );

    if (webhookResponse.status >= 400) {
      res.status(502).json({
        ok: false,
        message: "Register webhook returned an error.",
        provider,
        email,
        webhookStatus: webhookResponse.status,
        webhookBody: webhookResponse.data
      });
      return;
    }

    res.json({
      ok: true,
      message: "Register request sent successfully.",
      provider,
      email,
      webhookStatus: webhookResponse.status,
      webhookBody: webhookResponse.data
    });
  } catch (error) {
    res.status(502).json({
      ok: false,
      message: "Failed to reach register webhook.",
      provider,
      email,
      error: error.message
    });
  }
});

app.post("/doc-action", async (req, res) => {
  const action = typeof req.body?.action === "string" ? req.body.action.trim().toLowerCase() : "";
  const email = typeof req.body?.email === "string" ? req.body.email.trim() : "";
  const editedData = req.body?.editedData;
  const rowData = req.body?.rowData;

  if (!currentExportDocWebhookUrl) {
    res.status(500).json({
      ok: false,
      message: "EXPORT_DOC_WEBHOOK_URL is not configured."
    });
    return;
  }

  if (!["check", "export", "exportandclear", "clearhistory", "edit", "delete"].includes(action)) {
    res.status(400).json({
      ok: false,
      message: "Unsupported action. Allowed: check, export, exportandclear, clearhistory, edit, delete."
    });
    return;
  }

  if (!email) {
    res.status(400).json({
      ok: false,
      message: "Email is required."
    });
    return;
  }

  try {
    const webhookResponse = await axios.post(
      currentExportDocWebhookUrl,
      {
        action,
        email,
        ...(action === "edit" ? { editedData } : {}),
        ...(action === "delete" ? { rowData } : {})
      },
      {
        headers: {
          "Content-Type": "application/json"
        },
        validateStatus: () => true
      }
    );

    if (webhookResponse.status >= 400) {
      res.status(502).json({
        ok: false,
        message: "Export/check webhook returned an error.",
        action,
        email,
        webhookStatus: webhookResponse.status,
        webhookBody: webhookResponse.data
      });
      return;
    }

    const successMessageByAction = {
      check: "Check action sent successfully.",
      export: "Export action sent successfully.",
      exportandclear: "Export and Clear action sent successfully.",
      clearhistory: "Clear History action sent successfully.",
      edit: "Edit action sent successfully.",
      delete: "Delete row action sent successfully."
    };

    res.json({
      ok: true,
      message: action === "check" ? "ส่งคำสั่งตรวจสอบแล้ว" : "ส่งคำสั่ง Export and Clear แล้ว",
      message: successMessageByAction[action] || "Action sent successfully.",
      action,
      email,
      webhookStatus: webhookResponse.status,
      webhookBody: webhookResponse.data
    });
  } catch (error) {
    res.status(502).json({
      ok: false,
      message: "Failed to send action to export/check webhook.",
      action,
      email,
      error: error.message
    });
  }
});

app.post("/web-command", async (req, res) => {
  const command = typeof req.body?.message === "string" ? req.body.message.trim() : "";
  const node = typeof req.body?.node === "string" ? req.body.node.trim() : "";

  if (!currentCommandWebhookUrl) {
    res.status(500).json({
      ok: false,
      message: "COMMAND_WEBHOOK_URL is not configured."
    });
    return;
  }

  if (!command) {
    res.status(400).json({
      ok: false,
      message: "Command message is required."
    });
    return;
  }

  if (!node) {
    res.status(400).json({
      ok: false,
      message: "Node is required."
    });
    return;
  }

  try {
    const webhookResponse = await axios.post(
      currentCommandWebhookUrl,
      {
        message: command,
        node
      },
      {
        headers: {
          "Content-Type": "application/json"
        },
        validateStatus: () => true
      }
    );

    if (webhookResponse.status >= 400) {
      res.status(502).json({
        ok: false,
        message: "Command webhook returned an error.",
        command,
        node,
        webhookStatus: webhookResponse.status,
        webhookBody: webhookResponse.data
      });
      return;
    }

    res.json({
      ok: true,
      message: "Command sent successfully.",
      command,
      node,
      webhookStatus: webhookResponse.status,
      webhookBody: webhookResponse.data
    });
  } catch (error) {
    res.status(502).json({
      ok: false,
      message: "Failed to reach command webhook.",
      command,
      node,
      error: error.message
    });
  }
});

app.get("/web-command/options", async (req, res) => {
  if (!currentCommandWebhookUrl) {
    res.status(500).json({
      ok: false,
      message: "COMMAND_WEBHOOK_URL is not configured."
    });
    return;
  }

  try {
    const webhookResponse = await axios.post(
      currentCommandWebhookUrl,
      {
        message: "เรียก node",
        node: "node"
      },
      {
        headers: {
          "Content-Type": "application/json"
        },
        validateStatus: () => true
      }
    );

    if (webhookResponse.status >= 400) {
      res.status(502).json({
        ok: false,
        message: "Command webhook options request returned an error.",
        webhookStatus: webhookResponse.status,
        webhookBody: webhookResponse.data
      });
      return;
    }

    const rawItems = Array.isArray(webhookResponse.data)
      ? webhookResponse.data
      : webhookResponse.data && typeof webhookResponse.data === "object"
        ? [webhookResponse.data]
        : [];

    const items = rawItems
      .filter((item) => item && typeof item === "object")
      .map((item) => ({
        node: typeof item.node === "string" ? item.node.trim() : "",
        action: typeof item.action === "string" ? item.action.trim() : "",
        id: item.id ?? null,
        createdAt: item.createdAt ?? null,
        updatedAt: item.updatedAt ?? null
      }))
      .filter((item) => item.node);

    const nodes = [...new Set(items.map((item) => item.node))];

    res.json({
      ok: true,
      nodes,
      items,
      webhookStatus: webhookResponse.status,
      webhookBody: webhookResponse.data
    });
  } catch (error) {
    res.status(502).json({
      ok: false,
      message: "Failed to load command nodes.",
      error: error.message
    });
  }
});

async function processUploadJob({ uploadRequestId, files, formFields }) {
  const submittedAt = new Date().toISOString();
  const batchId = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  const originalDocumentNames = files
    .map((file) => repairMultipartFilename(file.originalname || ""))
    .filter(Boolean);

  runtimeStats.batchAttempts += 1;
  runtimeStats.lastUploadAt = submittedAt;
  runtimeStats.lastError = null;
  console.log(`[upload] preparing ${files.length} incoming file(s) before webhook send`);
  setUploadProgress(uploadRequestId, {
    phase: "preparing",
    sourceOriginalName: originalDocumentNames[0] || "",
    fileCount: files.length,
    pageCount: 0,
    preparedPages: 0,
    sentPages: 0,
    currentPageNumber: null,
    outboundFileCount: 0,
    error: ""
  });

  let outboundFiles = [];

  try {
    for (const file of files) {
      const processedFiles = await preprocessForOutboundFiles({
        ...file,
        uploadRequestId
      });
      outboundFiles = outboundFiles.concat(processedFiles);
    }
  } catch (error) {
    runtimeStats.failedBatches += 1;
    runtimeStats.lastFailureAt = new Date().toISOString();
    runtimeStats.lastWebhookStatus = "PREPROCESS_ERROR";
    runtimeStats.lastError = error.message;
    console.error(`[preprocess] failure: ${error.message}`);
    finalizeUploadProgress(uploadRequestId, {
      phase: "failed",
      error: error.message,
      result: {
        ok: false,
        message: "Failed to preprocess files for OCR.",
        error: error.message
      }
    });
    return;
  }

  console.log(
    `[upload] forwarding ${outboundFiles.length} outbound file(s) to webhook individually`
  );

  const results = [];

  for (const [index, file] of outboundFiles.entries()) {
    const form = new FormData();
    const fileNumber = index + 1;
    const sentAt = new Date().toISOString();

    runtimeStats.uploadAttempts += 1;
    setUploadProgress(uploadRequestId, {
      phase: file.processedKind.startsWith("pdf_page")
        ? "forwarding_pdf_pages"
        : file.processedKind.startsWith("image_ocr")
          ? "forwarding_image_ocr"
          : "forwarding_original",
      sourceOriginalName: file.sourceOriginalName,
      pageCount: file.pageCount || outboundFiles.length,
      preparedPages: outboundFiles.length,
      sentPages: index,
      currentPageNumber: file.pageNumber || fileNumber,
      outboundFileCount: outboundFiles.length
    });

    form.append("file", file.buffer, {
      filename: file.filename,
      contentType: file.mimeType,
      knownLength: file.size
    });
    form.append("submittedAt", submittedAt);
    form.append("sentAt", sentAt);
    form.append("batchId", batchId);
    form.append("fileIndex", String(fileNumber));
    form.append("fileCount", String(files.length));
    form.append("outboundFileCount", String(outboundFiles.length));
    form.append("originalName", file.filename);
    form.append("mimeType", file.mimeType);
    form.append("fileSize", String(file.size));
    form.append("sourceOriginalName", file.sourceOriginalName);
    form.append(
      "sourceOriginalNameUtf8Base64",
      Buffer.from(file.sourceOriginalName || "", "utf8").toString("base64")
    );
    form.append("processedKind", file.processedKind);
    form.append("sourcePageNumber", file.pageNumber ? String(file.pageNumber) : "");
    form.append("sourcePageCount", file.pageCount ? String(file.pageCount) : "");
    form.append("ocrCandidate", file.ocrCandidate || "");
    form.append("ocrConfidence", file.ocrConfidence != null ? String(file.ocrConfidence) : "");
    form.append("ocrScore", file.ocrScore != null ? String(file.ocrScore) : "");
    form.append("ocrTextLength", file.ocrTextLength != null ? String(file.ocrTextLength) : "");
    form.append("documentProfile", file.documentProfile || "");
    form.append(
      "backgroundSaturation",
      file.backgroundSaturation != null ? String(file.backgroundSaturation) : ""
    );
    form.append("tintStrength", file.tintStrength != null ? String(file.tintStrength) : "");

    for (const [key, value] of Object.entries(formFields || {})) {
      form.append(key, value);
    }

    try {
      const webhookResponse = await axios.post(currentWebhookUrl, form, {
        headers: form.getHeaders(),
        maxBodyLength: Infinity,
        maxContentLength: Infinity,
        validateStatus: () => true
      });

      if (webhookResponse.status >= 400) {
        runtimeStats.failedForwards += 1;
        runtimeStats.lastFailureAt = new Date().toISOString();
        runtimeStats.lastWebhookStatus = webhookResponse.status;
        runtimeStats.lastError = `n8n webhook returned HTTP ${webhookResponse.status}`;
        console.warn(
          `[upload] file ${fileNumber}/${outboundFiles.length} failed: ${file.filename} -> HTTP ${webhookResponse.status}`
        );
        results.push({
          ok: false,
          fileIndex: fileNumber,
          originalName: file.filename,
          sourceOriginalName: file.sourceOriginalName,
          processedKind: file.processedKind,
          sourcePageNumber: file.pageNumber,
          ocrCandidate: file.ocrCandidate,
          ocrConfidence: file.ocrConfidence,
          documentProfile: file.documentProfile,
          webhookStatus: webhookResponse.status,
          webhookBody: webhookResponse.data
        });
        setUploadProgress(uploadRequestId, {
          sentPages: fileNumber,
          currentPageNumber: file.pageNumber || fileNumber
        });
        continue;
      }

      runtimeStats.successfulForwards += 1;
      runtimeStats.lastSuccessAt = new Date().toISOString();
      runtimeStats.lastWebhookStatus = webhookResponse.status;
      runtimeStats.lastError = null;
      console.log(
        `[upload] file ${fileNumber}/${outboundFiles.length} success: ${file.filename} -> HTTP ${webhookResponse.status}`
      );
      results.push({
        ok: true,
        fileIndex: fileNumber,
        originalName: file.filename,
        sourceOriginalName: file.sourceOriginalName,
        processedKind: file.processedKind,
        sourcePageNumber: file.pageNumber,
        ocrCandidate: file.ocrCandidate,
        ocrConfidence: file.ocrConfidence,
        documentProfile: file.documentProfile,
        webhookStatus: webhookResponse.status,
        webhookBody: webhookResponse.data
      });
      setUploadProgress(uploadRequestId, {
        sentPages: fileNumber,
        currentPageNumber: file.pageNumber || fileNumber
      });
    } catch (error) {
      runtimeStats.failedForwards += 1;
      runtimeStats.lastFailureAt = new Date().toISOString();
      runtimeStats.lastWebhookStatus = "NETWORK_ERROR";
      runtimeStats.lastError = error.message;
      console.error(
        `[upload] file ${fileNumber}/${outboundFiles.length} network failure: ${file.filename} -> ${error.message}`
      );
      results.push({
        ok: false,
        fileIndex: fileNumber,
        originalName: file.filename,
        sourceOriginalName: file.sourceOriginalName,
        processedKind: file.processedKind,
        sourcePageNumber: file.pageNumber,
        ocrCandidate: file.ocrCandidate,
        ocrConfidence: file.ocrConfidence,
        documentProfile: file.documentProfile,
        webhookStatus: "NETWORK_ERROR",
        error: error.message
      });
      setUploadProgress(uploadRequestId, {
        sentPages: fileNumber,
        currentPageNumber: file.pageNumber || fileNumber
      });
    }
  }

  const successCount = results.filter((result) => result.ok).length;
  const failureCount = results.length - successCount;
  const allSucceeded = failureCount === 0;

  if (allSucceeded) {
    runtimeStats.successfulBatches += 1;
    try {
      await appendUploadHistory(originalDocumentNames, formFields.senderEmail);
    } catch (error) {
      console.warn(`[history] failed to save upload history: ${error.message}`);
    }
  } else {
    runtimeStats.failedBatches += 1;
  }

  const finalResult = {
    ok: allSucceeded,
    message: allSucceeded
      ? "Files forwarded to n8n one by one successfully."
      : "Some files could not be forwarded to n8n.",
    batchId,
    fileCount: files.length,
    outboundFileCount: outboundFiles.length,
    successCount,
    failureCount,
    results,
    uploadedDocumentNames: allSucceeded ? originalDocumentNames : []
  };

  finalizeUploadProgress(uploadRequestId, {
    phase: allSucceeded ? "completed" : "completed_with_errors",
    sourceOriginalName: originalDocumentNames[0] || "",
    pageCount: outboundFiles.length,
    preparedPages: outboundFiles.length,
    sentPages: outboundFiles.length,
    currentPageNumber: outboundFiles.length || null,
    outboundFileCount: outboundFiles.length,
    error: allSucceeded ? "" : "Some files could not be forwarded to n8n.",
    result: finalResult
  });
}

app.post("/upload", upload.array("files", maxFiles), async (req, res) => {
  try {
    const uploadRequestId =
      typeof req.body?.uploadRequestId === "string" ? req.body.uploadRequestId.trim() : "";

    if (!currentWebhookUrl) {
      runtimeStats.validationFailures += 1;
      runtimeStats.lastFailureAt = new Date().toISOString();
      runtimeStats.lastError = "N8N_WEBHOOK_URL is not configured.";
      console.warn("[upload] rejected: webhook is not configured");
      res.status(500).json({
        ok: false,
        message: "N8N_WEBHOOK_URL is not configured."
      });
      return;
    }

    if (!req.files || req.files.length === 0) {
      runtimeStats.validationFailures += 1;
      runtimeStats.lastFailureAt = new Date().toISOString();
      runtimeStats.lastError = "Please upload at least one file.";
      console.warn("[upload] rejected: no files uploaded");
      res.status(400).json({
        ok: false,
        message: "Please upload at least one file."
      });
      return;
    }

    const totalUploadBytes = req.files.reduce((total, file) => total + file.size, 0);

    if (totalUploadBytes > maxTotalUploadBytes) {
      runtimeStats.validationFailures += 1;
      runtimeStats.lastFailureAt = new Date().toISOString();
      runtimeStats.lastError = `Total upload size must be ${maxTotalUploadMb} MB or smaller.`;
      console.warn(
        `[upload] rejected: total size ${totalUploadBytes} bytes exceeds ${maxTotalUploadBytes} bytes`
      );
      res.status(400).json({
        ok: false,
        message: `Total upload size must be ${maxTotalUploadMb} MB or smaller.`
      });
      return;
    }

    const files = req.files.map((file) => ({
      originalname: file.originalname,
      mimetype: file.mimetype,
      size: file.size,
      buffer: Buffer.from(file.buffer)
    }));
    const formFields = Object.fromEntries(
      Object.entries(req.body || {}).filter(([key]) => key !== "uploadRequestId")
    );

    setUploadProgress(uploadRequestId, {
      phase: "queued",
      sourceOriginalName: repairMultipartFilename(files[0]?.originalname || ""),
      fileCount: files.length,
      pageCount: 0,
      preparedPages: 0,
      sentPages: 0,
      currentPageNumber: null,
      outboundFileCount: 0,
      error: ""
    });

    void processUploadJob({
      uploadRequestId,
      files,
      formFields
    }).catch((error) => {
      runtimeStats.validationFailures += 1;
      runtimeStats.lastFailureAt = new Date().toISOString();
      runtimeStats.lastError = error.message || "Unexpected upload error.";
      runtimeStats.lastWebhookStatus = "UNEXPECTED_UPLOAD_ERROR";
      console.error(`[upload] background job failure: ${runtimeStats.lastError}`);
      finalizeUploadProgress(uploadRequestId, {
        phase: "failed",
        error: runtimeStats.lastError,
        result: {
          ok: false,
          message: "Unexpected upload error.",
          error: runtimeStats.lastError
        }
      });
    });

    res.status(202).json({
      ok: true,
      accepted: true,
      uploadRequestId,
      message: "Upload accepted and processing started."
    });
  } catch (error) {
    runtimeStats.validationFailures += 1;
    runtimeStats.lastFailureAt = new Date().toISOString();
    runtimeStats.lastError = error.message || "Unexpected upload error.";
    runtimeStats.lastWebhookStatus = "UNEXPECTED_UPLOAD_ERROR";
    console.error(`[upload] unexpected failure: ${runtimeStats.lastError}`);
    res.status(500).json({
      ok: false,
      message: "Unexpected upload error.",
      error: runtimeStats.lastError
    });
  }
});

app.use((error, req, res, next) => {
  if (error instanceof multer.MulterError) {
    if (error.code === "LIMIT_FILE_SIZE") {
      runtimeStats.validationFailures += 1;
      runtimeStats.lastFailureAt = new Date().toISOString();
      runtimeStats.lastError = `Each file must be ${maxFileSizeMb} MB or smaller.`;
      console.warn(`[upload] rejected: file exceeded ${maxFileSizeMb} MB`);
      res.status(400).json({
        ok: false,
        message: `Each file must be ${maxFileSizeMb} MB or smaller.`
      });
      return;
    }

    if (error.code === "LIMIT_FILE_COUNT") {
      runtimeStats.validationFailures += 1;
      runtimeStats.lastFailureAt = new Date().toISOString();
      runtimeStats.lastError = `Maximum ${maxFiles} files are allowed per upload.`;
      console.warn(`[upload] rejected: file count exceeded ${maxFiles}`);
      res.status(400).json({
        ok: false,
        message: `Maximum ${maxFiles} files are allowed per upload.`
      });
      return;
    }
  }

  runtimeStats.validationFailures += 1;
  runtimeStats.lastFailureAt = new Date().toISOString();
  runtimeStats.lastError = error.message || "Upload failed.";
  console.warn(`[upload] rejected: ${runtimeStats.lastError}`);
  res.status(400).json({
    ok: false,
    message: error.message || "Upload failed."
  });
});

Promise.allSettled([loadWebhookSettings(), loadUploadHistory()])
  .then((results) => {
    for (const result of results) {
      if (result.status === "rejected") {
        const message = result.reason?.message || "Unknown initialization error";
        console.warn(`[startup] initialization warning: ${message}`);
      }
    }
  })
  .finally(() => {
    app.listen(port, host, () => {
      console.log(`Upload bridge listening on ${host}:${port}`);
      for (const url of getListenUrls()) {
        console.log(`Available URL: ${url}`);
      }
    });
  });
