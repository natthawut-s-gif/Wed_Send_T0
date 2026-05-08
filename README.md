# n8n Upload Webhook Bridge

หน้าเว็บ public สำหรับอัปโหลดหลายไฟล์ แล้วส่งต่อเข้า `n8n webhook`

รองรับไฟล์:

- PDF
- PNG
- JPG / JPEG
- Word (`.doc`, `.docx`)
- Excel (`.xls`, `.xlsx`)
- CSV

## วิธีใช้

1. ติดตั้ง dependencies

```bash
npm install
pip install -r requirements.txt
```

2. สร้างไฟล์ `.env` จาก `.env.example`

```bash
copy .env.example .env
```

3. แก้ค่า `N8N_WEBHOOK_URL`

```env
PORT=3000
N8N_WEBHOOK_URL=https://your-n8n-domain/webhook/upload-files
MAX_FILES=10
MAX_FILE_SIZE_MB=10
MAX_TOTAL_UPLOAD_MB=15
PYTHON_COMMAND=python
```

4. รันเซิร์ฟเวอร์

```bash
npm start
```

5. เปิดใช้งานที่:

```text
http://localhost:3000
```

ตั้งค่า webhook จากหน้าเว็บได้โดยกดปุ่ม `ตั้งค่า` มุมขวาบน

- `Webhook URL` ใช้สำหรับอัปโหลดไฟล์
- `Action Webhook URL` ใช้กับปุ่ม `ตรวจสอบ` และ `Export and Clear`
- ระบบจะบันทึกค่าล่าสุดไว้ใน `webhook-settings.json`

หรือใช้ตัวช่วย Python สำหรับเปิด/ปิดเว็บ:

```bash
python manage_web.py start
python manage_web.py status
python manage_web.py stop
```

ถ้าต้องการ UI สำหรับกดเปิด/ปิดและ monitor สถานะเว็บ:

```bash
python manage_web_ui.py
```

ในหน้าต่าง UI จะมี:

- ปุ่ม `Start`, `Stop`, `Restart`
- สถานะ process, PID, URL, health
- ตรวจว่า `N8N_WEBHOOK_URL` ถูกตั้งแล้วหรือยัง
- แสดง limit ของการอัปโหลดจาก `/health`
- ดู log ล่าสุดของเซิร์ฟเวอร์แบบ auto refresh
- กราฟ monitor สำหรับ health latency, memory, reachability, และ upload counters

## รูปแบบข้อมูลที่ส่งไป n8n

แอปจะ preprocess ก่อนส่งดังนี้:

- PDF: split เป็นรายหน้า, render เป็นภาพ, แล้วปรับภาพให้เหมาะกับ OCR
- PNG / JPG: ปรับภาพให้เหมาะกับ OCR ก่อนส่ง
- Word / Excel / CSV: ส่งไฟล์ต้นฉบับตามเดิม

หลังจากนั้นแอปจะส่ง `multipart/form-data` ไปยัง webhook แบบ `1 request ต่อ 1 ไฟล์`

แต่ละ request จะมี field หลักดังนี้:

- `file` ไฟล์เดียวต่อ request
- `batchId`
- `fileIndex`
- `submittedAt`
- `sentAt`
- `fileCount`
- `originalName`
- `mimeType`
- `fileSize`
- `sourceOriginalName`
- `processedKind`
- `sourcePageNumber`
- `sourcePageCount`
- field อื่นจากฟอร์ม เช่น `senderName`, `senderEmail`, `note`

## ทำให้เป็น Public URL

ตัวเลือกที่ง่าย:

### 1. Railway

- push โปรเจกต์นี้ขึ้น GitHub
- สร้างโปรเจกต์ใหม่ใน Railway
- ต้องมีทั้ง Node.js และ Python runtime เพราะมี OCR preprocessing ก่อนส่งไฟล์
- ตั้ง Environment Variable `N8N_WEBHOOK_URL`
- Deploy
- Railway จะให้ public URL เช่น `https://your-app.up.railway.app`

### 2. Render

- สร้าง Web Service จาก repo นี้
- Build Command: `npm install && pip install -r requirements.txt`
- Start Command: `npm start`
- ต้องมี Python ใช้งานได้จากคำสั่ง `python` หรือกำหนด `PYTHON_COMMAND`
- ตั้ง `N8N_WEBHOOK_URL`
- Deploy แล้วใช้ public URL ของ Render ได้ทันที

### 3. ชั่วคราวด้วย Cloudflare Tunnel / ngrok

ถ้าต้องการทดสอบเร็ว ๆ:

```bash
cloudflared tunnel --url http://localhost:3000
```

หรือ

```bash
ngrok http 3000
```

### 4. ใช้งานจากที่อื่นแบบง่ายที่สุดด้วย Quick Tunnel

ถ้าไม่ต้องการตั้งค่าโดเมนหรือ nameserver ให้ใช้ Quick Tunnel ได้ทันที:

```bash
python manage_web.py start
python manage_web.py quick-share-start
```

จากนั้นระบบจะคืน public URL แบบ `https://...trycloudflare.com`

ตรวจสอบหรือหยุด:

```bash
python manage_web.py quick-share-status
python manage_web.py quick-share-stop
```

หรือใช้ `python manage_web_ui.py` แล้วกดปุ่มในส่วน `Quick Share Tunnel`

ถ้าต้องการให้รอ public URL นานขึ้น ปรับได้ใน `.env`:

```env
QUICK_TUNNEL_WAIT_SECONDS=600
```

## หมายเหตุฝั่ง n8n

- Webhook node ควรรับ `multipart/form-data`
- ถ้าต้องใช้ไฟล์ต่อใน workflow ให้ตรวจใน binary data ของ webhook node ตามชื่อ `file`
- ถ้าอัปโหลด PDF เข้าเว็บ ปลายทางจะได้รับเป็นไฟล์ภาพรายหน้าแทน PDF ต้นฉบับ
- ถ้าอัปโหลด JPG / PNG เข้าเว็บ ปลายทางจะได้รับภาพที่ผ่าน OCR preprocessing แล้ว
- ถ้า webhook ของคุณตอบ status `4xx` หรือ `5xx` แอปนี้จะแสดง error กลับบนหน้าเว็บ
- เอกสาร n8n ระบุว่า webhook มี max payload default `16MB` ถ้า self-host อาจปรับเพิ่มได้ด้วย `N8N_PAYLOAD_SIZE_MAX`

## Docker Install And Run

ถ้าต้องการรันโปรเจกต์นี้ด้วย Docker ให้ใช้ขั้นตอนนี้

### Requirements

- Docker Engine หรือ Docker Desktop
- Docker Compose v2

### 1. Clone Project

```bash
git clone https://github.com/natthawut-s-gif/Wed_Send_T0.git
cd Wed_Send_T0
```

### 2. สร้างไฟล์ `.env`

```bash
cp .env.example .env
```

บน Windows:

```powershell
copy .env.example .env
```

จากนั้นแก้ค่าอย่างน้อย:

```env
N8N_WEBHOOK_URL=https://your-n8n-domain/webhook/upload-files
EXPORT_DOC_WEBHOOK_URL=https://your-n8n-domain/webhook/export_doc_fj_project
COMMAND_WEBHOOK_URL=https://your-n8n-domain/webhook/commands
```

### 3. Build และ Run

```bash
docker compose up -d --build
```

### 4. เปิดใช้งาน

```text
http://localhost:3000
```

### 5. ตรวจสอบสถานะ

```bash
docker compose ps
docker compose logs -f
```

### 6. หยุดการทำงาน

```bash
docker compose down
```

### ข้อมูลที่ Docker จะสร้างให้เอง

เมื่อเริ่มครั้งแรก ระบบจะสร้างไฟล์ในโฟลเดอร์ `data` อัตโนมัติ:

- `data/webhook-settings.json`
- `data/upload-history.json`

ไฟล์เหล่านี้ใช้เก็บค่า webhook และประวัติอัปโหลด และจะยังอยู่แม้ container restart

## Docker Update

ถ้าคุณรันแบบ build จาก source code ในเครื่อง:

```bash
git pull
docker compose up -d --build
```

ถ้าต้องการ restart อย่างเดียว:

```bash
docker compose restart
```

## Docker Run From Published Image

ถ้าต้องการให้ server รันจาก image ที่ build มาจาก GitHub repo นี้โดยตรง ให้ใช้:

```bash
docker compose -f docker-compose.server.yml up -d
```

ไฟล์นี้จะใช้ image:

```text
ghcr.io/natthawut-s-gif/wed_send_t0:latest
```

และถ้า image บน GitHub Container Registry ถูกอัปเดต `watchtower` จะช่วยดึง image ใหม่และ restart container ให้อัตโนมัติ

ตรวจสอบ:

```bash
docker compose -f docker-compose.server.yml ps
docker compose -f docker-compose.server.yml logs -f
```

หยุด:

```bash
docker compose -f docker-compose.server.yml down
```
