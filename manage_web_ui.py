import contextlib
import io
import threading
import tkinter as tk
import tkinter.scrolledtext as scrolledtext
import tkinter.simpledialog as simpledialog
from collections import deque
from datetime import datetime
from tkinter import ttk

from cloudflared_manager import get_setup_commands
from cloudflared_manager import get_quick_tunnel_snapshot
from cloudflared_manager import get_tunnel_snapshot
from manage_web import LOG_FILE
from manage_web import UPDATE_LOG_FILE
from manage_web import find_next_available_port
from manage_web import get_port_binding_status
from manage_web import get_status_snapshot
from manage_web import open_site
from cloudflared_manager import open_quick_tunnel_url
from manage_web import read_env_port
from manage_web import read_env_flag
from manage_web import read_log_tail
from manage_web import read_env_value
from manage_web import read_update_log_tail
from manage_web import save_local_server_settings
from cloudflared_manager import open_public_url
from cloudflared_manager import read_quick_tunnel_log_tail
from cloudflared_manager import read_log_tail as read_tunnel_log_tail
from cloudflared_manager import restart_quick_tunnel
from cloudflared_manager import restart_tunnel
from cloudflared_manager import save_settings as save_tunnel_settings
from cloudflared_manager import start_quick_tunnel
from cloudflared_manager import start_tunnel
from cloudflared_manager import stop_quick_tunnel
from cloudflared_manager import stop_tunnel
from manage_web import restart_server
from manage_web import start_server
from manage_web import stop_server
from manage_web import update_project


HISTORY_LIMIT = 60
POLL_INTERVAL_MS = 2000


class MonitorChart(ttk.Frame):
    def __init__(
        self,
        parent: ttk.Frame,
        title: str,
        *,
        unit: str = "",
        fixed_min: float = 0.0,
        fixed_max: float | None = None,
    ) -> None:
        super().__init__(parent, padding=10, style="Card.TFrame")
        self.unit = unit
        self.fixed_min = fixed_min
        self.fixed_max = fixed_max
        self.series: list[tuple[str, str, list[float | None]]] = []

        header = ttk.Frame(self, style="Card.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text=title, style="Value.TLabel").pack(side="left")
        self.latest_text = tk.StringVar(value="No data")
        ttk.Label(header, textvariable=self.latest_text, style="Body.TLabel").pack(side="right")

        self.canvas = tk.Canvas(
            self,
            height=180,
            bg="#fffdf8",
            highlightthickness=1,
            highlightbackground="#e2d4c2",
        )
        self.canvas.pack(fill="both", expand=True, pady=(8, 0))
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

    def set_series(self, series: list[tuple[str, str, list[float | None]]]) -> None:
        self.series = series
        latest_parts = []
        for label, _color, values in self.series:
            latest_value = next((value for value in reversed(values) if value is not None), None)
            if latest_value is None:
                latest_parts.append(f"{label}: -")
            else:
                latest_parts.append(f"{label}: {self._format_value(latest_value)}")
        self.latest_text.set(" | ".join(latest_parts) if latest_parts else "No data")
        self.redraw()

    def _format_value(self, value: float) -> str:
        if self.fixed_max == 1.0 and self.unit == "":
            return "Up" if value >= 1 else "Down"
        if abs(value) >= 100:
            text = f"{value:.0f}"
        elif abs(value) >= 10:
            text = f"{value:.1f}"
        else:
            text = f"{value:.2f}"
        return f"{text}{self.unit}"

    def redraw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")

        width = max(canvas.winfo_width(), 240)
        height = max(canvas.winfo_height(), 180)
        left = 16
        right = width - 16
        top = 14
        bottom = height - 26
        plot_width = max(right - left, 1)
        plot_height = max(bottom - top, 1)

        values = [
            value
            for _label, _color, series_values in self.series
            for value in series_values
            if value is not None
        ]

        if not values:
            canvas.create_text(
                width / 2,
                height / 2,
                text="No metric data yet",
                fill="#7a6d61",
                font=("Segoe UI", 10),
            )
            return

        y_min = self.fixed_min
        y_max = self.fixed_max if self.fixed_max is not None else max(values)
        if y_max <= y_min:
            y_max = y_min + 1
        else:
            y_max = y_max * 1.08 if self.fixed_max is None else y_max

        for step in range(5):
            y = top + (plot_height * step / 4)
            canvas.create_line(left, y, right, y, fill="#eee2d5")

        canvas.create_rectangle(left, top, right, bottom, outline="#e2d4c2")

        max_points = max(len(series_values) for _label, _color, series_values in self.series)
        x_step = plot_width / max(max_points - 1, 1)

        for label, color, series_values in self.series:
            segments: list[list[float]] = []
            current_segment: list[float] = []
            last_point = None

            for index, value in enumerate(series_values):
                if value is None:
                    if current_segment:
                        segments.append(current_segment)
                        current_segment = []
                    continue

                x = left + index * x_step
                y = bottom - ((value - y_min) / (y_max - y_min)) * plot_height
                current_segment.extend([x, y])
                last_point = (x, y)

            if current_segment:
                segments.append(current_segment)

            for points in segments:
                if len(points) >= 4:
                    canvas.create_line(points, fill=color, width=2, smooth=True)
                elif len(points) == 2:
                    x, y = points
                    canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline=color)

            if last_point:
                x, y = last_point
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline=color)

        canvas.create_text(
            left + 2,
            top - 2,
            anchor="sw",
            text=self._format_value(y_max if self.fixed_max is not None else max(values)),
            fill="#7a6d61",
            font=("Segoe UI", 8),
        )
        canvas.create_text(
            left + 2,
            bottom + 2,
            anchor="nw",
            text=self._format_value(y_min),
            fill="#7a6d61",
            font=("Segoe UI", 8),
        )


class WebMonitorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Upload Web Monitor")
        self.root.geometry("1240x920")
        self.root.minsize(1100, 760)

        self.show_update_project = read_env_flag("SHOW_UPDATE_PROJECT_BUTTON", default=False)
        self.update_project_password = read_env_value("UPDATE_PROJECT_PASSWORD", "")
        self.update_project_unlocked = False
        self.configured_port = read_env_port()
        self.is_busy = False
        self.refresh_in_progress = False
        self.last_log_text = ""
        self.last_update_log_text = ""
        self.auto_refresh = tk.BooleanVar(value=True)
        self.open_browser_on_start = tk.BooleanVar(value=True)
        self.local_port_var = tk.StringVar(value=str(self.configured_port))
        self.local_port_status_text = tk.StringVar(value="-")
        self.local_port_preview_text = tk.StringVar(value=f"http://localhost:{self.configured_port}/")

        self.status_text = tk.StringVar(value="Checking...")
        self.pid_text = tk.StringVar(value="-")
        self.url_text = tk.StringVar(value="-")
        self.share_url_text = tk.StringVar(value="-")
        self.health_text = tk.StringVar(value="-")
        self.webhook_text = tk.StringVar(value="-")
        self.limits_text = tk.StringVar(value="-")
        self.updated_text = tk.StringVar(value="-")
        self.uptime_text = tk.StringVar(value="-")
        self.memory_text = tk.StringVar(value="-")
        self.uploads_text = tk.StringVar(value="-")
        self.success_rate_text = tk.StringVar(value="-")
        self.webhook_status_text = tk.StringVar(value="-")
        self.log_size_text = tk.StringVar(value="-")
        self.tunnel_status_text = tk.StringVar(value="-")
        self.tunnel_pid_text = tk.StringVar(value="-")
        self.tunnel_public_url_text = tk.StringVar(value="-")
        self.quick_tunnel_status_text = tk.StringVar(value="-")
        self.quick_tunnel_pid_text = tk.StringVar(value="-")
        self.quick_tunnel_url_text = tk.StringVar(value="-")
        self.tunnel_mode_text = tk.StringVar(value="-")
        self.tunnel_metrics_text = tk.StringVar(value="-")
        self.tunnel_config_text = tk.StringVar(value="-")
        self.tunnel_executable_text = tk.StringVar(value="-")
        self.tunnel_hostname_var = tk.StringVar(value="")
        self.tunnel_id_var = tk.StringVar(value="")
        self.tunnel_name_var = tk.StringVar(value="")
        self.tunnel_credentials_var = tk.StringVar(value="")
        self.tunnel_service_var = tk.StringVar(value="")
        self.tunnel_metrics_var = tk.StringVar(value="")
        self.tunnel_executable_var = tk.StringVar(value="")
        self.tunnel_protocol_var = tk.StringVar(value="")
        self.last_tunnel_log_text = ""
        self.last_quick_tunnel_log_text = ""
        self.tunnel_entries: list[ttk.Entry] = []
        self.local_port_entry: ttk.Entry | None = None

        self.latency_history: deque[float | None] = deque(maxlen=HISTORY_LIMIT)
        self.memory_history: deque[float | None] = deque(maxlen=HISTORY_LIMIT)
        self.reachability_history: deque[float | None] = deque(maxlen=HISTORY_LIMIT)
        self.attempt_history: deque[float | None] = deque(maxlen=HISTORY_LIMIT)
        self.success_history: deque[float | None] = deque(maxlen=HISTORY_LIMIT)
        self.failure_history: deque[float | None] = deque(maxlen=HISTORY_LIMIT)
        self.validation_history: deque[float | None] = deque(maxlen=HISTORY_LIMIT)

        self._configure_styles()
        self.local_port_var.trace_add("write", self._on_local_port_changed)
        self._build_ui()
        self.request_refresh()
        self.schedule_refresh()

    def _configure_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self.root.configure(bg="#efe7da")
        style.configure("Card.TFrame", background="#fffaf4")
        style.configure("Shell.TFrame", background="#efe7da")
        style.configure("Title.TLabel", background="#fffaf4", font=("Segoe UI", 20, "bold"))
        style.configure("Body.TLabel", background="#fffaf4", font=("Segoe UI", 10))
        style.configure("Value.TLabel", background="#fffaf4", font=("Segoe UI", 10, "bold"))
        style.configure("Body.TCheckbutton", background="#fffaf4", font=("Segoe UI", 10))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=16, style="Shell.TFrame")
        shell.pack(fill="both", expand=True)

        card = ttk.Frame(shell, padding=18, style="Card.TFrame")
        card.pack(fill="both", expand=True)

        title_row = ttk.Frame(card, style="Card.TFrame")
        title_row.pack(fill="x")

        ttk.Label(title_row, text="Web Control Panel", style="Title.TLabel").pack(side="left")
        ttk.Button(
            title_row,
            text="Open Website",
            command=open_site,
            style="Accent.TButton",
        ).pack(side="right")

        ttk.Label(
            card,
            text="Live controls, health telemetry, runtime counters, and recent logs in one window.",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(6, 14))

        controls = ttk.Frame(card, style="Card.TFrame")
        controls.pack(fill="x", pady=(0, 14))

        self.update_unlock_button = None
        if self.show_update_project:
            self.update_unlock_button = ttk.Button(
                controls,
                text=" ",
                width=2,
                command=self.prompt_update_project_password,
            )
            self.update_unlock_button.pack(side="left", padx=(0, 8))

        self.start_button = ttk.Button(
            controls,
            text="Start",
            command=lambda: self.run_action(
                "Start server",
                start_server,
                self.open_browser_on_start.get(),
            ),
            style="Accent.TButton",
        )
        self.start_button.pack(side="left", padx=(0, 8))

        self.stop_button = ttk.Button(
            controls,
            text="Stop",
            command=lambda: self.run_action("Stop server", stop_server),
        )
        self.stop_button.pack(side="left", padx=(0, 8))

        self.restart_button = ttk.Button(
            controls,
            text="Restart",
            command=lambda: self.run_action(
                "Restart server",
                restart_server,
                self.open_browser_on_start.get(),
            ),
        )
        self.restart_button.pack(side="left", padx=(0, 8))

        self.update_button = ttk.Button(
            controls,
            text="Update Project",
            command=lambda: self.run_action("Push project to Git", update_project),
        )
        if self.update_project_unlocked:
            self.update_button.pack(side="left", padx=(0, 8))

        self.refresh_now_button = ttk.Button(
            controls,
            text="Refresh Now",
            command=self.request_refresh,
        )
        self.refresh_now_button.pack(side="left", padx=(0, 12))

        ttk.Checkbutton(
            controls,
            text="Auto refresh",
            variable=self.auto_refresh,
            style="Body.TCheckbutton",
        ).pack(side="left", padx=(0, 12))

        ttk.Checkbutton(
            controls,
            text="Open browser on start",
            variable=self.open_browser_on_start,
            style="Body.TCheckbutton",
        ).pack(side="left")

        local_server_card = ttk.Frame(card, padding=14, style="Card.TFrame")
        local_server_card.pack(fill="x", pady=(0, 14))

        ttk.Label(local_server_card, text="Local Server", style="Value.TLabel").pack(anchor="w")
        ttk.Label(
            local_server_card,
            text=(
                "Set the local web port here. If the port is already used, "
                "Start will switch to the next free port automatically."
            ),
            style="Body.TLabel",
        ).pack(anchor="w", pady=(6, 10))

        local_server_controls = ttk.Frame(local_server_card, style="Card.TFrame")
        local_server_controls.pack(fill="x", pady=(0, 8))

        ttk.Label(local_server_controls, text="Local Port", style="Body.TLabel").pack(side="left")
        self.local_port_entry = ttk.Entry(
            local_server_controls,
            textvariable=self.local_port_var,
            width=10,
        )
        self.local_port_entry.pack(side="left", padx=(10, 8))
        self.save_local_port_button = ttk.Button(
            local_server_controls,
            text="Save Port Settings",
            command=self.save_local_port_settings_from_ui,
        )
        self.save_local_port_button.pack(side="left", padx=(0, 8))
        self.use_next_local_port_button = ttk.Button(
            local_server_controls,
            text="Use Next Free Port",
            command=self.use_next_free_port_from_ui,
        )
        self.use_next_local_port_button.pack(side="left")

        local_server_status = ttk.Frame(local_server_card, style="Card.TFrame")
        local_server_status.pack(fill="x")
        local_server_status.columnconfigure(1, weight=1)
        local_server_status.columnconfigure(3, weight=1)
        self._add_status_row(local_server_status, 0, 0, "Preview URL", self.local_port_preview_text)
        self._add_status_row(local_server_status, 0, 2, "Port Status", self.local_port_status_text)

        tunnel_card = ttk.Frame(card, padding=14, style="Card.TFrame")
        tunnel_card.pack(fill="x", pady=(0, 14))

        tunnel_header = ttk.Frame(tunnel_card, style="Card.TFrame")
        tunnel_header.pack(fill="x")
        ttk.Label(tunnel_header, text="Cloudflare Tunnel", style="Value.TLabel").pack(side="left")
        ttk.Button(
            tunnel_header,
            text="Open Public URL",
            command=open_public_url,
        ).pack(side="right")

        ttk.Label(
            tunnel_card,
            text=(
                "Manage a real public hostname such as doc.yourdomain.com. "
                "Update the fields below, save, then start the tunnel."
            ),
            style="Body.TLabel",
        ).pack(anchor="w", pady=(6, 10))

        tunnel_controls = ttk.Frame(tunnel_card, style="Card.TFrame")
        tunnel_controls.pack(fill="x", pady=(0, 10))

        self.tunnel_start_button = ttk.Button(
            tunnel_controls,
            text="Start Tunnel",
            command=lambda: self.run_action("Start Cloudflare Tunnel", start_tunnel),
            style="Accent.TButton",
        )
        self.tunnel_start_button.pack(side="left", padx=(0, 8))

        self.tunnel_stop_button = ttk.Button(
            tunnel_controls,
            text="Stop Tunnel",
            command=lambda: self.run_action("Stop Cloudflare Tunnel", stop_tunnel),
        )
        self.tunnel_stop_button.pack(side="left", padx=(0, 8))

        self.tunnel_restart_button = ttk.Button(
            tunnel_controls,
            text="Restart Tunnel",
            command=lambda: self.run_action("Restart Cloudflare Tunnel", restart_tunnel),
        )
        self.tunnel_restart_button.pack(side="left", padx=(0, 8))

        self.save_tunnel_settings_button = ttk.Button(
            tunnel_controls,
            text="Save Tunnel Settings",
            command=self.save_tunnel_settings_from_ui,
        )
        self.save_tunnel_settings_button.pack(side="left")

        quick_tunnel_card = ttk.Frame(card, padding=14, style="Card.TFrame")
        quick_tunnel_card.pack(fill="x", pady=(0, 14))

        quick_header = ttk.Frame(quick_tunnel_card, style="Card.TFrame")
        quick_header.pack(fill="x")
        ttk.Label(quick_header, text="Quick Share Tunnel", style="Value.TLabel").pack(side="left")
        ttk.Button(
            quick_header,
            text="Open Quick URL",
            command=open_quick_tunnel_url,
        ).pack(side="right")

        ttk.Label(
            quick_tunnel_card,
            text=(
                "Use this when you only need to open the web from another location. "
                "No custom domain required."
            ),
            style="Body.TLabel",
        ).pack(anchor="w", pady=(6, 10))

        quick_controls = ttk.Frame(quick_tunnel_card, style="Card.TFrame")
        quick_controls.pack(fill="x", pady=(0, 8))

        self.quick_tunnel_start_button = ttk.Button(
            quick_controls,
            text="Start Quick Share",
            command=lambda: self.run_action("Start Quick Tunnel", start_quick_tunnel),
            style="Accent.TButton",
        )
        self.quick_tunnel_start_button.pack(side="left", padx=(0, 8))

        self.quick_tunnel_stop_button = ttk.Button(
            quick_controls,
            text="Stop Quick Share",
            command=lambda: self.run_action("Stop Quick Tunnel", stop_quick_tunnel),
        )
        self.quick_tunnel_stop_button.pack(side="left", padx=(0, 8))

        self.quick_tunnel_restart_button = ttk.Button(
            quick_controls,
            text="Restart Quick Share",
            command=lambda: self.run_action("Restart Quick Tunnel", restart_quick_tunnel),
        )
        self.quick_tunnel_restart_button.pack(side="left")

        quick_status_grid = ttk.Frame(quick_tunnel_card, style="Card.TFrame")
        quick_status_grid.pack(fill="x")
        quick_status_grid.columnconfigure(1, weight=1)
        quick_status_grid.columnconfigure(3, weight=1)
        self._add_status_row(quick_status_grid, 0, 0, "Quick Tunnel", self.quick_tunnel_status_text)
        self._add_status_row(quick_status_grid, 0, 2, "Quick PID", self.quick_tunnel_pid_text)
        self._add_status_row(quick_status_grid, 1, 0, "Quick URL", self.quick_tunnel_url_text)

        tunnel_form = ttk.Frame(tunnel_card, style="Card.TFrame")
        tunnel_form.pack(fill="x")
        tunnel_form.columnconfigure(1, weight=1)
        tunnel_form.columnconfigure(3, weight=1)

        self._add_entry_row(tunnel_form, 0, 0, "Hostname", self.tunnel_hostname_var)
        self._add_entry_row(tunnel_form, 0, 2, "Tunnel ID", self.tunnel_id_var)
        self._add_entry_row(tunnel_form, 1, 0, "Tunnel Name", self.tunnel_name_var)
        self._add_entry_row(tunnel_form, 1, 2, "Protocol", self.tunnel_protocol_var)
        self._add_entry_row(tunnel_form, 2, 0, "Service URL", self.tunnel_service_var)
        self._add_entry_row(tunnel_form, 2, 2, "Metrics", self.tunnel_metrics_var)
        self._add_entry_row(tunnel_form, 3, 0, "Credentials File", self.tunnel_credentials_var)
        self._add_entry_row(tunnel_form, 3, 2, "Executable Path", self.tunnel_executable_var)

        setup_frame = ttk.Frame(tunnel_card, style="Card.TFrame")
        setup_frame.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(setup_frame, text="Tunnel Setup Commands", style="Value.TLabel").pack(anchor="w")
        self.tunnel_commands_text = scrolledtext.ScrolledText(
            setup_frame,
            height=5,
            wrap="word",
            font=("Consolas", 10),
            bg="#fffdf8",
        )
        self.tunnel_commands_text.pack(fill="both", expand=True, pady=(6, 0))
        self.tunnel_commands_text.configure(state="disabled")

        status_grid = ttk.Frame(card, style="Card.TFrame")
        status_grid.pack(fill="x", pady=(0, 14))
        status_grid.columnconfigure(1, weight=1)
        status_grid.columnconfigure(3, weight=1)

        self._add_status_row(status_grid, 0, 0, "Process", self.status_text)
        self._add_status_row(status_grid, 0, 2, "PID", self.pid_text)
        self._add_status_row(status_grid, 1, 0, "URL", self.url_text)
        self._add_status_row(status_grid, 1, 2, "Health", self.health_text)
        self._add_status_row(status_grid, 2, 0, "Share URL", self.share_url_text)
        self._add_status_row(status_grid, 2, 2, "Webhook", self.webhook_text)
        self._add_status_row(status_grid, 3, 0, "Webhook Status", self.webhook_status_text)
        self._add_status_row(status_grid, 3, 2, "Uptime", self.uptime_text)
        self._add_status_row(status_grid, 4, 0, "Memory", self.memory_text)
        self._add_status_row(status_grid, 4, 2, "Uploads", self.uploads_text)
        self._add_status_row(status_grid, 5, 0, "Success Rate", self.success_rate_text)
        self._add_status_row(status_grid, 5, 2, "Limits", self.limits_text)
        self._add_status_row(status_grid, 6, 0, "Tunnel Mode", self.tunnel_mode_text)
        self._add_status_row(status_grid, 6, 2, "Tunnel", self.tunnel_status_text)
        self._add_status_row(status_grid, 7, 0, "Tunnel PID", self.tunnel_pid_text)
        self._add_status_row(status_grid, 7, 2, "Public URL", self.tunnel_public_url_text)
        self._add_status_row(status_grid, 8, 0, "Tunnel Metrics", self.tunnel_metrics_text)
        self._add_status_row(status_grid, 8, 2, "Tunnel Config", self.tunnel_config_text)
        self._add_status_row(status_grid, 9, 0, "Tunnel Executable", self.tunnel_executable_text)
        self._add_status_row(status_grid, 9, 2, "Log Size", self.log_size_text)
        self._add_status_row(status_grid, 10, 0, "Updated", self.updated_text)

        chart_grid = ttk.Frame(card, style="Card.TFrame")
        chart_grid.pack(fill="both", expand=False, pady=(0, 14))
        chart_grid.columnconfigure(0, weight=1)
        chart_grid.columnconfigure(1, weight=1)
        chart_grid.rowconfigure(0, weight=1)
        chart_grid.rowconfigure(1, weight=1)

        self.latency_chart = MonitorChart(chart_grid, "Health Latency", unit="ms")
        self.latency_chart.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))

        self.memory_chart = MonitorChart(chart_grid, "Node Memory", unit="MB")
        self.memory_chart.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))

        self.reachability_chart = MonitorChart(
            chart_grid,
            "Reachability",
            fixed_min=0.0,
            fixed_max=1.0,
        )
        self.reachability_chart.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(8, 0))

        self.uploads_chart = MonitorChart(chart_grid, "Upload Counters")
        self.uploads_chart.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(8, 0))

        self.panes = ttk.Panedwindow(card, orient="vertical")
        self.panes.pack(fill="both", expand=True)

        activity_frame = ttk.Frame(self.panes, padding=6, style="Card.TFrame")
        self.panes.add(activity_frame, weight=1)
        ttk.Label(activity_frame, text="Action Output", style="Value.TLabel").pack(anchor="w")
        self.activity_text = scrolledtext.ScrolledText(
            activity_frame,
            height=8,
            wrap="word",
            font=("Consolas", 10),
            bg="#fffdf8",
        )
        self.activity_text.pack(fill="both", expand=True, pady=(6, 0))
        self.activity_text.insert("1.0", "Ready.\n")
        self.activity_text.configure(state="disabled")

        self.update_log_text = None
        self.update_log_frame = ttk.Frame(self.panes, padding=6, style="Card.TFrame")
        update_log_header = ttk.Frame(self.update_log_frame, style="Card.TFrame")
        update_log_header.pack(fill="x")
        ttk.Label(update_log_header, text="Project Update Log", style="Value.TLabel").pack(side="left")
        ttk.Label(update_log_header, text=str(UPDATE_LOG_FILE), style="Body.TLabel").pack(side="right")

        self.update_log_text = scrolledtext.ScrolledText(
            self.update_log_frame,
            height=10,
            wrap="none",
            font=("Consolas", 10),
            bg="#fffdf8",
        )
        self.update_log_text.pack(fill="both", expand=True, pady=(6, 0))
        self.update_log_text.configure(state="disabled")

        if self.update_project_unlocked:
            self.panes.add(self.update_log_frame, weight=1)

        log_frame = ttk.Frame(self.panes, padding=6, style="Card.TFrame")
        self.panes.add(log_frame, weight=2)
        log_header = ttk.Frame(log_frame, style="Card.TFrame")
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Recent Server Log", style="Value.TLabel").pack(side="left")
        ttk.Label(log_header, text=str(LOG_FILE), style="Body.TLabel").pack(side="right")

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=14,
            wrap="none",
            font=("Consolas", 10),
            bg="#fffdf8",
        )
        self.log_text.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text.configure(state="disabled")

        tunnel_log_frame = ttk.Frame(self.panes, padding=6, style="Card.TFrame")
        self.panes.add(tunnel_log_frame, weight=2)
        tunnel_log_header = ttk.Frame(tunnel_log_frame, style="Card.TFrame")
        tunnel_log_header.pack(fill="x")
        ttk.Label(tunnel_log_header, text="Recent Tunnel Log", style="Value.TLabel").pack(side="left")
        self.tunnel_log_path_label = ttk.Label(tunnel_log_header, text="-", style="Body.TLabel")
        self.tunnel_log_path_label.pack(side="right")

        self.tunnel_log_text = scrolledtext.ScrolledText(
            tunnel_log_frame,
            height=12,
            wrap="none",
            font=("Consolas", 10),
            bg="#fffdf8",
        )
        self.tunnel_log_text.pack(fill="both", expand=True, pady=(6, 0))
        self.tunnel_log_text.configure(state="disabled")

        quick_tunnel_log_frame = ttk.Frame(self.panes, padding=6, style="Card.TFrame")
        self.panes.add(quick_tunnel_log_frame, weight=1)
        quick_tunnel_log_header = ttk.Frame(quick_tunnel_log_frame, style="Card.TFrame")
        quick_tunnel_log_header.pack(fill="x")
        ttk.Label(quick_tunnel_log_header, text="Recent Quick Tunnel Log", style="Value.TLabel").pack(side="left")
        self.quick_tunnel_log_path_label = ttk.Label(quick_tunnel_log_header, text="-", style="Body.TLabel")
        self.quick_tunnel_log_path_label.pack(side="right")

        self.quick_tunnel_log_text = scrolledtext.ScrolledText(
            quick_tunnel_log_frame,
            height=10,
            wrap="none",
            font=("Consolas", 10),
            bg="#fffdf8",
        )
        self.quick_tunnel_log_text.pack(fill="both", expand=True, pady=(6, 0))
        self.quick_tunnel_log_text.configure(state="disabled")

    def _add_status_row(
        self,
        parent: ttk.Frame,
        row: int,
        column: int,
        label_text: str,
        value_var: tk.StringVar,
    ) -> None:
        ttk.Label(parent, text=label_text, style="Body.TLabel").grid(
            row=row,
            column=column,
            sticky="w",
            padx=(0, 10),
            pady=4,
        )
        ttk.Label(parent, textvariable=value_var, style="Value.TLabel").grid(
            row=row,
            column=column + 1,
            sticky="w",
            padx=(0, 18),
            pady=4,
        )

    def _add_entry_row(
        self,
        parent: ttk.Frame,
        row: int,
        column: int,
        label_text: str,
        variable: tk.StringVar,
    ) -> None:
        ttk.Label(parent, text=label_text, style="Body.TLabel").grid(
            row=row,
            column=column,
            sticky="w",
            padx=(0, 10),
            pady=4,
        )
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(
            row=row,
            column=column + 1,
            sticky="ew",
            padx=(0, 18),
            pady=4,
        )
        self.tunnel_entries.append(entry)

    def _on_local_port_changed(self, *_args) -> None:
        raw_value = self.local_port_var.get().strip()
        if not raw_value:
            self.local_port_preview_text.set("-")
            return

        if raw_value.isdigit():
            self.local_port_preview_text.set(f"http://localhost:{raw_value}/")
        else:
            self.local_port_preview_text.set("Invalid port")

    def append_activity(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"[{timestamp}] {message.strip()}\n"
        self.activity_text.configure(state="normal")
        self.activity_text.insert("end", text)
        self.activity_text.see("end")
        self.activity_text.configure(state="disabled")

    def prompt_update_project_password(self) -> None:
        if not self.show_update_project:
            return
        if self.update_project_unlocked:
            self.append_activity("Update Project is already unlocked for this session.")
            return
        if not self.update_project_password:
            self.append_activity("UPDATE_PROJECT_PASSWORD is not configured in .env.")
            return

        password = simpledialog.askstring(
            "Unlock Update Project",
            "Enter password",
            parent=self.root,
            show="*",
        )
        if password is None:
            return
        if password != self.update_project_password:
            self.append_activity("Update Project password is incorrect.")
            return

        self.update_project_unlocked = True
        self.append_activity("Update Project unlocked for this session.")
        self.show_update_project_controls()

    def show_update_project_controls(self) -> None:
        if not self.update_project_unlocked:
            return

        if self.update_button is not None and not self.update_button.winfo_manager():
            self.update_button.pack(side="left", padx=(0, 8), before=self.refresh_now_button)

        if self.update_log_frame is not None:
            managed = any(str(child) == str(self.update_log_frame) for child in self.panes.panes())
            if not managed:
                self.panes.insert(1, self.update_log_frame, weight=1)

    def _tunnel_form_has_focus(self) -> bool:
        focused_widget = self.root.focus_get()
        return focused_widget in self.tunnel_entries

    def _local_port_has_focus(self) -> bool:
        focused_widget = self.root.focus_get()
        return focused_widget == self.local_port_entry

    def set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.update_unlock_button,
            self.start_button,
            self.stop_button,
            self.restart_button,
            self.update_button,
            self.save_local_port_button,
            self.use_next_local_port_button,
            self.tunnel_start_button,
            self.tunnel_stop_button,
            self.tunnel_restart_button,
            self.quick_tunnel_start_button,
            self.quick_tunnel_stop_button,
            self.quick_tunnel_restart_button,
            self.save_tunnel_settings_button,
        ):
            if button is not None:
                button.configure(state=state)

    def run_action(self, label: str, action, *args) -> None:
        if self.is_busy:
            return

        self.set_busy(True)
        self.append_activity(f"{label}...")

        thread = threading.Thread(
            target=self._run_action_thread,
            args=(label, action, args),
            daemon=True,
        )
        thread.start()

    def _run_action_thread(self, label: str, action, args: tuple) -> None:
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                exit_code = action(*args)
        except Exception as error:  # noqa: BLE001
            output = buffer.getvalue().strip()
            summary = output or f"{label} failed."
            summary = f"{summary}\nUnhandled error: {error}"
            self.root.after(0, self._finish_action, summary, 1)
            return

        output = buffer.getvalue().strip()
        summary = output or f"{label} finished with exit code {exit_code}."
        self.root.after(0, self._finish_action, summary, exit_code)

    def _finish_action(self, summary: str, exit_code: int) -> None:
        suffix = "OK" if exit_code == 0 else f"Failed ({exit_code})"
        self.append_activity(f"{summary}\nResult: {suffix}")
        self.set_busy(False)
        self.request_refresh()

    def request_refresh(self) -> None:
        if self.refresh_in_progress:
            return

        self.refresh_in_progress = True
        thread = threading.Thread(target=self._refresh_thread, daemon=True)
        thread.start()

    def _refresh_thread(self) -> None:
        snapshot = get_status_snapshot()
        tunnel_snapshot = get_tunnel_snapshot()
        quick_tunnel_snapshot = get_quick_tunnel_snapshot()
        log_text = read_log_tail(180) or "(No log output yet.)"
        update_log_text = read_update_log_tail(180) or "(No project update log yet.)"
        tunnel_log_text = read_tunnel_log_tail(180) or "(No tunnel log output yet.)"
        quick_tunnel_log_text = read_quick_tunnel_log_tail(180) or "(No quick tunnel log output yet.)"
        self.root.after(
            0,
            self._apply_snapshot,
            snapshot,
            tunnel_snapshot,
            quick_tunnel_snapshot,
            log_text,
            update_log_text,
            tunnel_log_text,
            quick_tunnel_log_text,
        )

    def _apply_snapshot(
        self,
        snapshot: dict,
        tunnel_snapshot: dict,
        quick_tunnel_snapshot: dict,
        log_text: str,
        update_log_text: str,
        tunnel_log_text: str,
        quick_tunnel_log_text: str,
    ) -> None:
        self.refresh_in_progress = False
        health = snapshot["health"]
        health_body = health.get("body") or {}
        runtime = health_body.get("runtime") or {}
        stats = runtime.get("stats") or {}
        memory_mb = snapshot.get("process", {}).get("memory_mb")
        latency_ms = health.get("latency_ms") if health.get("reachable") else None
        reachable_value = 1.0 if health.get("reachable") else 0.0

        attempts = stats.get("uploadAttempts", 0)
        successes = stats.get("successfulForwards", 0)
        failures = stats.get("failedForwards", 0)
        validation_failures = stats.get("validationFailures", 0)
        total_finished = successes + failures
        success_rate = (successes / total_finished * 100) if total_finished else 0.0
        configured_port = snapshot.get("port") or read_env_port()
        port_status = snapshot.get("port_status") or {}

        self.status_text.set("Running" if snapshot["running"] else "Stopped")
        self.pid_text.set(str(snapshot["pid"] or "-"))
        self.url_text.set(snapshot["url"])
        if not self._local_port_has_focus():
            self.local_port_var.set(str(configured_port))
        self.local_port_preview_text.set(f"http://localhost:{configured_port}/")
        self.local_port_status_text.set(self._format_port_status(port_status, snapshot["running"]))
        share_urls = snapshot.get("share_urls") or []
        self.share_url_text.set(" | ".join(share_urls) if share_urls else "-")

        if health.get("reachable"):
            self.health_text.set(
                f"Healthy ({health.get('status_code')}) | {health.get('latency_ms')} ms"
            )
        else:
            self.health_text.set(f"Unavailable: {health.get('error')}")

        webhook_configured = health_body.get("webhookConfigured")
        if webhook_configured is None:
            self.webhook_text.set("-")
        else:
            self.webhook_text.set("Configured" if webhook_configured else "Missing N8N_WEBHOOK_URL")

        if health_body:
            self.limits_text.set(
                "files={files}, single={single}MB, total={total}MB".format(
                    files=health_body.get("maxFiles", "-"),
                    single=health_body.get("maxFileSizeMb", "-"),
                    total=health_body.get("maxTotalUploadMb", "-"),
                )
            )
        else:
            self.limits_text.set("-")

        uptime_seconds = runtime.get("uptimeSeconds")
        self.uptime_text.set(self._format_duration(uptime_seconds) if uptime_seconds is not None else "-")
        self.memory_text.set(f"{memory_mb:.2f} MB" if memory_mb is not None else "-")
        self.uploads_text.set(
            f"attempts={attempts} | success={successes} | failed={failures} | validation={validation_failures}"
        )
        self.success_rate_text.set(f"{success_rate:.1f}%")
        self.webhook_status_text.set(str(stats.get("lastWebhookStatus") or "-"))
        self.log_size_text.set(f"{snapshot.get('log_size_kb', 0):.2f} KB")
        self.updated_text.set(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._apply_tunnel_snapshot(tunnel_snapshot)
        self._apply_quick_tunnel_snapshot(quick_tunnel_snapshot)
        self._apply_tunnel_mode_snapshot(tunnel_snapshot, quick_tunnel_snapshot)

        self._append_history(self.latency_history, latency_ms)
        self._append_history(self.memory_history, memory_mb)
        self._append_history(self.reachability_history, reachable_value)
        self._append_history(self.attempt_history, float(attempts))
        self._append_history(self.success_history, float(successes))
        self._append_history(self.failure_history, float(failures))
        self._append_history(self.validation_history, float(validation_failures))

        self.latency_chart.set_series([
            ("Latency", "#b95c32", list(self.latency_history)),
        ])
        self.memory_chart.set_series([
            ("Memory", "#2f6c8f", list(self.memory_history)),
        ])
        self.reachability_chart.set_series([
            ("Status", "#1d6b57", list(self.reachability_history)),
        ])
        self.uploads_chart.set_series([
            ("Attempts", "#3a4e73", list(self.attempt_history)),
            ("Success", "#1d6b57", list(self.success_history)),
            ("Failed", "#a33232", list(self.failure_history)),
            ("Validation", "#b27722", list(self.validation_history)),
        ])

        self.refresh_log(log_text)
        self.refresh_update_log(update_log_text)
        self.refresh_tunnel_log(tunnel_log_text)
        self.refresh_quick_tunnel_log(quick_tunnel_log_text)

    def _apply_tunnel_snapshot(self, tunnel_snapshot: dict) -> None:
        metrics = tunnel_snapshot.get("metrics", {})
        settings = tunnel_snapshot.get("settings", {})

        if tunnel_snapshot.get("running"):
            self.tunnel_status_text.set("Running")
        elif tunnel_snapshot.get("validation_errors"):
            self.tunnel_status_text.set("Config incomplete")
        else:
            self.tunnel_status_text.set("Stopped")
        self.tunnel_pid_text.set(str(tunnel_snapshot.get("pid") or "-"))
        self.tunnel_public_url_text.set(tunnel_snapshot.get("public_url", "-"))
        self.tunnel_config_text.set(tunnel_snapshot.get("config_path", "-"))
        self.tunnel_executable_text.set(
            tunnel_snapshot.get("executable_path") or "cloudflared not found"
        )
        self.tunnel_log_path_label.configure(text=tunnel_snapshot.get("log_path", "-"))

        if metrics.get("reachable"):
            self.tunnel_metrics_text.set(
                "Healthy ({status}) | {latency} ms | metrics={count}".format(
                    status=metrics.get("status_code"),
                    latency=metrics.get("latency_ms"),
                    count=metrics.get("metrics_count"),
                )
            )
        else:
            self.tunnel_metrics_text.set(f"Unavailable: {metrics.get('error')}")

        if not self._tunnel_form_has_focus():
            self.tunnel_hostname_var.set(settings.get("hostname", ""))
            self.tunnel_id_var.set(settings.get("tunnel_id", ""))
            self.tunnel_name_var.set(settings.get("tunnel_name", ""))
            self.tunnel_credentials_var.set(settings.get("credentials_file", ""))
            self.tunnel_service_var.set(settings.get("service_url", ""))
            self.tunnel_metrics_var.set(settings.get("metrics_address", ""))
            self.tunnel_executable_var.set(settings.get("executable_path", ""))
            self.tunnel_protocol_var.set(settings.get("protocol", ""))
        self.refresh_tunnel_commands(settings)

    def _apply_quick_tunnel_snapshot(self, quick_tunnel_snapshot: dict) -> None:
        self.quick_tunnel_status_text.set("Running" if quick_tunnel_snapshot.get("running") else "Stopped")
        self.quick_tunnel_pid_text.set(str(quick_tunnel_snapshot.get("pid") or "-"))
        quick_public_url = quick_tunnel_snapshot.get("public_url") or "-"
        self.quick_tunnel_url_text.set(quick_public_url)
        self.quick_tunnel_log_path_label.configure(text=quick_tunnel_snapshot.get("log_path", "-"))

    def _apply_tunnel_mode_snapshot(
        self,
        tunnel_snapshot: dict,
        quick_tunnel_snapshot: dict,
    ) -> None:
        named_running = bool(tunnel_snapshot.get("running"))
        quick_running = bool(quick_tunnel_snapshot.get("running"))

        if named_running and quick_running:
            mode = "Both: Named + Quick Share"
        elif named_running:
            mode = "Named Tunnel"
        elif quick_running:
            mode = "Quick Share Tunnel"
        elif tunnel_snapshot.get("validation_errors"):
            mode = "No active tunnel | Named config incomplete"
        else:
            mode = "No active tunnel"

        self.tunnel_mode_text.set(mode)

    def _append_history(self, history: deque[float | None], value: float | None) -> None:
        history.append(value)

    def _format_port_status(self, port_status: dict, running: bool) -> str:
        if running and port_status.get("occupied_by_current_server"):
            return "In use by current server"
        if port_status.get("available"):
            return "Available"
        suggested_port = port_status.get("suggested_port")
        if suggested_port:
            return f"Busy | next free: {suggested_port}"
        return "Busy"

    def refresh_log(self, log_text: str) -> None:
        if log_text == self.last_log_text:
            return

        self.last_log_text = log_text
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", log_text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def refresh_update_log(self, log_text: str) -> None:
        if self.update_log_text is None:
            return
        if log_text == self.last_update_log_text:
            return

        self.last_update_log_text = log_text
        self.update_log_text.configure(state="normal")
        self.update_log_text.delete("1.0", "end")
        self.update_log_text.insert("1.0", log_text)
        self.update_log_text.see("end")
        self.update_log_text.configure(state="disabled")

    def refresh_tunnel_commands(self, settings: dict | None = None) -> None:
        commands_text = "\n".join(get_setup_commands(settings))
        self.tunnel_commands_text.configure(state="normal")
        self.tunnel_commands_text.delete("1.0", "end")
        self.tunnel_commands_text.insert("1.0", commands_text)
        self.tunnel_commands_text.configure(state="disabled")

    def refresh_tunnel_log(self, log_text: str) -> None:
        if log_text == self.last_tunnel_log_text:
            return

        self.last_tunnel_log_text = log_text
        self.tunnel_log_text.configure(state="normal")
        self.tunnel_log_text.delete("1.0", "end")
        self.tunnel_log_text.insert("1.0", log_text)
        self.tunnel_log_text.see("end")
        self.tunnel_log_text.configure(state="disabled")

    def refresh_quick_tunnel_log(self, log_text: str) -> None:
        if log_text == self.last_quick_tunnel_log_text:
            return

        self.last_quick_tunnel_log_text = log_text
        self.quick_tunnel_log_text.configure(state="normal")
        self.quick_tunnel_log_text.delete("1.0", "end")
        self.quick_tunnel_log_text.insert("1.0", log_text)
        self.quick_tunnel_log_text.see("end")
        self.quick_tunnel_log_text.configure(state="disabled")

    def save_local_port_settings_from_ui(self) -> None:
        try:
            settings = save_local_server_settings(self.local_port_var.get())
        except Exception as error:  # noqa: BLE001
            self.append_activity(f"Saving local port failed: {error}")
            return

        port_status = get_port_binding_status(settings["port"])
        self.local_port_status_text.set(self._format_port_status(port_status, running=False))
        self.append_activity(
            "Local port settings saved.\n"
            f"Local URL: {settings['url']}\n"
            "Restart the server to use the new port."
        )
        self.request_refresh()

    def use_next_free_port_from_ui(self) -> None:
        current_value = self.local_port_var.get().strip() or str(read_env_port())
        try:
            next_port = find_next_available_port(int(current_value) + 1)
        except ValueError:
            self.append_activity("Current local port is invalid.")
            return

        if not next_port:
            self.append_activity("No free replacement port was found.")
            return

        self.local_port_var.set(str(next_port))
        self.append_activity(f"Suggested next free port: {next_port}")

    def save_tunnel_settings_from_ui(self) -> None:
        try:
            settings = save_tunnel_settings(
                {
                    "hostname": self.tunnel_hostname_var.get(),
                    "tunnel_id": self.tunnel_id_var.get(),
                    "tunnel_name": self.tunnel_name_var.get(),
                    "credentials_file": self.tunnel_credentials_var.get(),
                    "service_url": self.tunnel_service_var.get(),
                    "metrics_address": self.tunnel_metrics_var.get(),
                    "executable_path": self.tunnel_executable_var.get(),
                    "protocol": self.tunnel_protocol_var.get(),
                }
            )
        except Exception as error:  # noqa: BLE001
            self.append_activity(f"Saving tunnel settings failed: {error}")
            return

        self.append_activity(
            "Tunnel settings saved.\n"
            f"Hostname: {settings.get('hostname')}\n"
            f"Config: {get_tunnel_snapshot().get('config_path')}"
        )
        self.refresh_tunnel_commands(settings)
        self.request_refresh()

    def schedule_refresh(self) -> None:
        if self.auto_refresh.get() and not self.is_busy:
            self.request_refresh()
        self.root.after(POLL_INTERVAL_MS, self.schedule_refresh)

    def _format_duration(self, total_seconds: int | None) -> str:
        if total_seconds is None:
            return "-"
        hours, remainder = divmod(int(total_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def main() -> None:
    root = tk.Tk()
    WebMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
