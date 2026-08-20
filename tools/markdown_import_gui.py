"""Windows Markdown 导入向导。

界面只负责收集用户确认和展示结果，解析与上传逻辑统一复用现有 CLI 客户端。
"""

from __future__ import annotations

import threading
import uuid
import webbrowser
import json
import os
import base64
import ctypes
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import tkinter as tk
from tkinter import messagebox, ttk

def normalize_site_url(value: str) -> str:
    """规范化站点地址，并为只填写主机的地址补默认语言前缀。"""
    raw = value.strip()
    if not raw:
        raise ValueError("网站地址不能为空")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("网站地址只支持 http 或 https")
    if not parts.hostname or parts.username or parts.password:
        raise ValueError("网站地址无效")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("网站端口无效") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("网站端口无效")
    path = parts.path.rstrip("/") or "/zh-hans"
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))


def admin_edit_url(site_url: str, page_id: int) -> str:
    """后台不在语言前缀下，使用站点 origin 拼接 Wagtail 编辑地址。"""
    parts = urlsplit(site_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/admin/pages/{int(page_id)}/edit/", "", ""))


def runtime_root() -> Path:
    """冻结运行时使用 EXE 目录，源码运行时使用当前工作目录。"""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def scan_markdown_files(root: Path) -> list[Path]:
    """只扫描根目录的 Markdown，避免误把子目录笔记批量导入。"""
    return sorted(
        (
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.casefold() == ".md"
        ),
        key=lambda path: path.name.casefold(),
    )


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


ACTIVE_CHECKPOINT_STATUSES = frozenset({"created", "uploading", "ready", "assembling"})


def checkpoint_reuse_candidate(checkpoint: dict | None) -> bool:
    """只有新版且仍在进行中的会话才允许在 GUI 中询问断点续传。"""
    return bool(
        checkpoint
        and not checkpoint.get("legacy")
        and checkpoint.get("session_status") in ACTIVE_CHECKPOINT_STATUSES
    )


def _credential_file() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WagtailMarkdownImporter"
    base.mkdir(parents=True, exist_ok=True)
    return base / "connection.json"


def _protect(value: str) -> str:
    if os.name != "nt":
        return ""
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
    data = value.encode("utf-8")
    in_buf = ctypes.create_string_buffer(data)
    in_blob = Blob(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise OSError("Windows DPAPI 加密失败")
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return base64.b64encode(raw).decode("ascii")


def _unprotect(value: str) -> str:
    if os.name != "nt":
        return ""
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
    raw = base64.b64decode(value)
    in_buf = ctypes.create_string_buffer(raw)
    in_blob = Blob(len(raw), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise OSError("Windows DPAPI 解密失败")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def load_connection() -> dict:
	try:
		payload = json.loads(_credential_file().read_text(encoding="utf-8"))
		if payload.get("token_encrypted"):
			payload["token"] = _unprotect(payload["token_encrypted"])
		return payload
	except (OSError, ValueError):
		return {}


def save_connection(site_url: str, token: str) -> None:
	# 本地配置文件仅供当前 Windows 用户使用，权限由用户配置目录继承。
	_credential_file().write_text(
		json.dumps({"site_url": site_url, "token_encrypted": _protect(token)}, ensure_ascii=False),
		encoding="utf-8",
	)


def format_inline_image_location(item: dict) -> str:
    """把稳定定位信息转换为用户可读位置，不显示本地绝对路径。"""
    return (
        f"表格 {int(item['table_index'])}，第 {int(item['row_index'])} 行，"
        f"第 {int(item['cell_index'])} 列"
    )


def safe_import_error(error: Exception) -> str:
    """仅显示短错误码，避免把本地绝对路径带到结果窗口。"""
    value = str(getattr(error, "code", "") or str(error)).strip()
    if not value or len(value) > 120 or "\\" in value or "/" in value:
        return error.__class__.__name__
    return value


def ai_template_options(templates: list[dict]) -> tuple[list[str], dict[str, int]]:
    """把服务端模板摘要转换为稳定下拉选项，不接收提示词正文。"""

    labels = []
    mapping = {}
    for item in templates:
        if not isinstance(item, dict):
            continue
        try:
            template_id = int(item["id"])
            version = int(item["version"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(item.get("name") or "").strip()
        if template_id <= 0 or version <= 0 or not name:
            continue
        label = f"{name}（v{version}）"
        labels.append(label)
        mapping[label] = template_id
    return labels, mapping


def apply_ai_suggestion(metadata: dict, suggestion: dict) -> dict:
    """只应用简介和标签，用户标题及其他逐文件字段保持不变。"""

    intro = suggestion.get("intro")
    tags = suggestion.get("tags")
    if not isinstance(intro, str) or not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("ai_suggestion_invalid")
    result = dict(metadata)
    result["intro"] = intro.strip()
    result["tags"] = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
    return result


class MarkdownImportApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Markdown 导入博客")
        self.geometry("900x720")
        self.minsize(860, 720)
        self.configure(bg="#f0f5f4")

        self.site_url = ""
        self.token = ""
        self.limits: dict = {}
        self.destinations: list[dict] = []
        self.target_parent_id: int | None = None
        self.root_dir = runtime_root()
        self.files: list[Path] = []
        self.selected_paths: list[Path] = []
        self.preview: dict | None = None
        self.previews: dict[Path, dict] = {}
        self.file_metadata: dict[Path, dict] = {}
        self.ai_templates: list[dict] = []
        self.ai_template_labels: list[str] = []
        self.ai_template_ids: dict[str, int] = {}
        self.ai_template_descriptions: dict[int, str] = {}
        self.ai_template_error = ""
        self.ai_busy = False
        self.duplicate_titles: list[dict] = []
        self.active_metadata_path: Path | None = None
        self._loading_metadata = False
        self.idempotency_key: str | None = None

        self._build_shell()
        self._build_config_page()
        self._build_destination_page()
        self._build_files_page()
        self._build_preview_page()
        self._build_result_page()
        self.show_page("config")

    def _build_shell(self):
        header = ttk.Frame(self, padding=(24, 18, 24, 8))
        header.pack(fill="x")
        ttk.Label(header, text="Markdown 导入博客", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text="连接站点，选择索引页，预检后创建未发布草稿",
            foreground="#475569",
        ).pack(anchor="w", pady=(3, 0))
        self.steps_label = ttk.Label(header, text="1 连接   2 索引页   3 Markdown   4 预检   5 结果", foreground="#0f766e")
        self.steps_label.pack(anchor="w", pady=(12, 0))

        body = ttk.Frame(self, padding=(24, 8, 24, 18))
        body.pack(fill="both", expand=True)
        self.page_container = body
        self.error_var = tk.StringVar()
        self.error_label = ttk.Label(body, textvariable=self.error_var, foreground="#b91c1c", wraplength=820)
        self.error_label.pack(fill="x", pady=(0, 8))
        self.pages: dict[str, ttk.Frame] = {}

    def _new_page(self, name: str) -> ttk.Frame:
        frame = ttk.Frame(self.page_container)
        self.pages[name] = frame
        return frame

    def _build_config_page(self):
        frame = self._new_page("config")
        ttk.Label(frame, text="连接站点", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(4, 16))
        form = ttk.Frame(frame)
        form.pack(fill="x")
        ttk.Label(form, text="网站地址").grid(row=0, column=0, sticky="w", pady=7)
        saved = load_connection()
        self.url_var = tk.StringVar(value=saved.get("site_url") or "http://192.168.20.5:8080")
        ttk.Entry(form, textvariable=self.url_var, width=58).grid(row=0, column=1, sticky="ew", padx=(16, 0), pady=7)
        ttk.Label(form, text="可填写 IP:端口，例如 192.168.20.5:8080；客户端会补 /zh-hans", foreground="#64748b").grid(
            row=1, column=1, sticky="w", padx=(16, 0)
        )
        ttk.Label(form, text="JWT Token").grid(row=2, column=0, sticky="w", pady=7)
        self.token_var = tk.StringVar(value=saved.get("token") or "")
        ttk.Entry(form, textvariable=self.token_var, width=58, show="*").grid(row=2, column=1, sticky="ew", padx=(16, 0), pady=7)
        self.remember_var = tk.BooleanVar(value=bool(saved.get("token")))
        ttk.Checkbutton(form, text="记住连接信息（仅当前 Windows 用户）", variable=self.remember_var).grid(row=3, column=1, sticky="w", padx=(16, 0), pady=4)
        form.columnconfigure(1, weight=1)
        self.connect_button = ttk.Button(frame, text="测试连接", command=self.connect_site)
        self.connect_button.pack(anchor="w", pady=(24, 0))
        self.config_status_var = tk.StringVar(value=f"扫描目录：{self.root_dir}")
        ttk.Label(frame, textvariable=self.config_status_var, foreground="#475569", wraplength=820).pack(anchor="w", pady=(18, 0))

    def _build_destination_page(self):
        frame = self._new_page("destinations")
        ttk.Label(frame, text="选择博客索引页", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(4, 12))
        ttk.Label(frame, text="只显示当前账号有权限创建子页面的索引页。", foreground="#475569").pack(anchor="w", pady=(0, 10))
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        self.destination_tree = ttk.Treeview(table_frame, columns=("id", "title"), show="headings", height=12)
        self.destination_tree.heading("id", text="ID")
        self.destination_tree.heading("title", text="标题")
        self.destination_tree.column("id", width=120, anchor="center")
        self.destination_tree.column("title", width=580, anchor="w")
        self.destination_tree.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(table_frame, orient="vertical", command=self.destination_tree.yview).pack(side="right", fill="y")
        self.destination_tree.bind("<<TreeviewSelect>>", self._destination_selected)
        self.destination_next = ttk.Button(frame, text="下一步：扫描 Markdown", command=self.open_files, state="disabled")
        self.destination_next.pack(anchor="w", pady=(14, 0))

    def _build_files_page(self):
        frame = self._new_page("files")
        ttk.Label(frame, text="选择 Markdown 文件", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(4, 12))
        self.files_status_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.files_status_var, foreground="#475569", wraplength=820).pack(anchor="w", pady=(0, 10))
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        self.files_tree = ttk.Treeview(table_frame, columns=("name", "size", "modified"), show="headings", height=14, selectmode="extended")
        for column, label, width in (("name", "文件名", 480), ("size", "大小", 120), ("modified", "修改时间", 180)):
            self.files_tree.heading(column, text=label)
            self.files_tree.column(column, width=width, anchor="w")
        self.files_tree.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(table_frame, orient="vertical", command=self.files_tree.yview).pack(side="right", fill="y")
        self.files_tree.bind("<<TreeviewSelect>>", self._file_selected)
        self.files_next = ttk.Button(frame, text="下一步：预检", command=self.preview_file, state="disabled")
        self.files_next.pack(anchor="w", pady=(14, 0))

    def _build_preview_page(self):
        frame = self._new_page("preview")
        ttk.Label(frame, text="预检与导入选项", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(4, 10))
        self.preview_status_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.preview_status_var, foreground="#0f766e", wraplength=820).pack(anchor="w", pady=(0, 8))
        ttk.Label(
            frame,
            text="每个文件都会创建一个独立 BlogPage；请在文件清单中逐篇确认标题、简介和标签。索引页对本次选择的所有文件生效。",
            foreground="#475569",
            wraplength=820,
        ).pack(anchor="w", pady=(0, 8))
        metadata_table = ttk.Frame(frame)
        metadata_table.pack(fill="x", pady=(0, 8))
        self.metadata_tree = ttk.Treeview(
            metadata_table,
            columns=("file", "title", "tags", "status"),
            show="headings",
            height=3,
            selectmode="browse",
        )
        for column, label, width in (
            ("file", "Markdown 文件", 260),
            ("title", "标题", 220),
            ("tags", "标签", 180),
            ("status", "预检状态", 120),
        ):
            self.metadata_tree.heading(column, text=label)
            self.metadata_tree.column(column, width=width, anchor="w")
        self.metadata_tree.pack(side="left", fill="x", expand=True)
        ttk.Scrollbar(metadata_table, orient="vertical", command=self.metadata_tree.yview).pack(side="right", fill="y")
        self.metadata_tree.bind("<<TreeviewSelect>>", self._metadata_selected)
        form = ttk.Frame(frame)
        form.pack(fill="x")
        ttk.Label(form, text="标题（当前文件）").grid(row=0, column=0, sticky="w", pady=4)
        self.title_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.title_var).grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(form, text="简介（当前文件，必填）").grid(row=1, column=0, sticky="w", pady=4)
        self.intro_var = tk.StringVar()
        self.intro_var.trace_add("write", lambda *_args: self._refresh_import_state())
        ttk.Entry(form, textvariable=self.intro_var).grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(form, text="日期").grid(row=2, column=0, sticky="w", pady=4)
        self.date_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.date_var).grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(form, text="标签（当前文件，逗号分隔）").grid(row=3, column=0, sticky="w", pady=4)
        self.tags_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.tags_var).grid(row=3, column=1, sticky="ew", padx=(12, 0), pady=4)
        for variable in (self.title_var, self.intro_var, self.date_var, self.tags_var):
            variable.trace_add("write", lambda *_args: self._metadata_field_changed())
        form.columnconfigure(1, weight=1)
        self.ai_consent_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            form,
            text="允许发送当前文章的受限纯文本以生成 AI 简介和标签",
            variable=self.ai_consent_var,
            command=self._ai_option_changed,
        ).grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(8, 4))
        ttk.Label(form, text="AI 提示词模板").grid(row=5, column=0, sticky="w", pady=4)
        ai_controls = ttk.Frame(form)
        ai_controls.grid(row=5, column=1, sticky="ew", padx=(12, 0), pady=4)
        self.ai_template_var = tk.StringVar()
        self.ai_template_select = ttk.Combobox(
            ai_controls,
            textvariable=self.ai_template_var,
            state="disabled",
        )
        self.ai_template_select.pack(side="left", fill="x", expand=True)
        self.ai_template_select.bind("<<ComboboxSelected>>", self._ai_option_changed)
        self.ai_generate_button = ttk.Button(
            ai_controls,
            text="生成建议",
            command=self.generate_current_ai,
            state="disabled",
        )
        self.ai_generate_button.pack(side="left", padx=(8, 0))
        self.ai_status_var = tk.StringVar()
        ttk.Label(form, textvariable=self.ai_status_var, foreground="#475569", wraplength=650).grid(
            row=6, column=1, sticky="w", padx=(12, 0), pady=(0, 4)
        )
        self.external_images_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="允许下载远程 HTTPS 图片（会上传到博客媒体库）", variable=self.external_images_var, command=self._refresh_import_state).grid(
            row=7, column=1, sticky="w", padx=(12, 0), pady=(8, 4)
        )
        self.preview_details = tk.Text(frame, height=4, wrap="word", state="disabled", background="#ffffff")
        self.preview_details.pack(fill="both", expand=True, pady=(12, 8))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="重新选择文件", command=lambda: self.show_page("files")).pack(side="left")
        self.import_button = ttk.Button(buttons, text="导入未发布草稿", command=self.start_import, state="disabled")
        self.import_button.pack(side="left", padx=(10, 0))

    def _build_result_page(self):
        frame = self._new_page("result")
        ttk.Label(frame, text="导入结果", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(4, 12))
        self.result_text = tk.Text(frame, height=16, wrap="word", state="disabled", background="#ffffff")
        self.result_text.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        self.open_admin_button = ttk.Button(buttons, text="打开后台编辑页", command=self.open_admin, state="disabled")
        self.open_admin_button.pack(side="left")
        ttk.Button(buttons, text="选择另一个 Markdown", command=self.open_files).pack(side="left", padx=(10, 0))

    def show_page(self, name: str):
        for frame in self.pages.values():
            frame.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.error_var.set("")

    def _show_error(self, error):
        self.error_var.set(str(error))

    def _run_async(self, worker, success, failure=None):
        self.error_var.set("")
        result = {}

        def run():
            try:
                result["value"] = worker()
            except Exception as exc:  # 将异常转回主线程，避免 Tk 控件跨线程更新
                result["error"] = exc
            self.after(0, finish)

        def finish():
            if "error" in result:
                if failure:
                    failure(result["error"])
                else:
                    self._show_error(result["error"])
            else:
                success(result["value"])

        threading.Thread(target=run, daemon=True).start()

    def connect_site(self):
        try:
            site_url = normalize_site_url(self.url_var.get())
        except ValueError as exc:
            self._show_error(exc)
            return
        token = self.token_var.get().strip()
        if not token:
            self._show_error("JWT Token 不能为空")
            return
        self.site_url, self.token = site_url, token
        if self.remember_var.get():
            save_connection(site_url, token)
        self.connect_button.configure(state="disabled")

        def worker():
            import requests

            headers = {"Authorization": f"Bearer {token}"}
            limits_response = requests.get(site_url.rstrip("/") + "/blog/api/markdown-import/limits/", headers=headers, timeout=15)
            destinations_response = requests.get(site_url.rstrip("/") + "/blog/api/markdown-import/destinations/", headers=headers, timeout=15)
            for response in (limits_response, destinations_response):
                if response.status_code >= 400:
                    try:
                        code = response.json().get("code", "请求失败")
                    except ValueError:
                        code = "请求失败"
                    raise RuntimeError(f"连接失败（HTTP {response.status_code}）：{code}")
            return limits_response.json(), destinations_response.json().get("destinations", [])

        def success(value):
            self.connect_button.configure(state="normal")
            self.limits, self.destinations = value
            for item in self.destination_tree.get_children():
                self.destination_tree.delete(item)
            for destination in self.destinations:
                self.destination_tree.insert("", "end", iid=str(destination["id"]), values=(destination["id"], destination["title"]))
            self.show_page("destinations")

        def failure(error):
            self.connect_button.configure(state="normal")
            self._show_error(error)

        self._run_async(worker, success, failure)

    def _destination_selected(self, _event=None):
        selected = self.destination_tree.selection()
        self.target_parent_id = int(selected[0]) if selected else None
        self.destination_next.configure(state="normal" if self.target_parent_id else "disabled")

    def open_files(self):
        self.files = scan_markdown_files(self.root_dir)
        self.files_status_var.set(f"目录：{self.root_dir}，找到 {len(self.files)} 个 Markdown 文件")
        for item in self.files_tree.get_children():
            self.files_tree.delete(item)
        for index, path in enumerate(self.files):
            stat = path.stat()
            self.files_tree.insert("", "end", iid=str(index), values=(path.name, format_size(stat.st_size), stat.st_mtime))
        self.selected_paths = []
        self.previews = {}
        self.file_metadata = {}
        self.ai_templates = []
        self.ai_template_labels = []
        self.ai_template_ids = {}
        self.ai_template_descriptions = {}
        self.ai_template_error = ""
        self.active_metadata_path = None
        self.files_next.configure(state="disabled")
        self.show_page("files")

    def _file_selected(self, _event=None):
        selected = self.files_tree.selection()
        self.selected_paths = [self.files[int(item)] for item in selected]
        self.files_next.configure(state="normal" if self.selected_paths else "disabled")

    def preview_file(self):
        if not self.selected_paths:
            return
        paths = list(self.selected_paths)
        self.preview_status_var.set(f"正在预检 {len(paths)} 个文件……")
        self.show_page("preview")

        def worker():
            try:
                from tools.markdown_import.client import fetch_ai_templates, inspect_markdown
            except ModuleNotFoundError:  # PyInstaller 将 tools/markdown_import 作为顶层包收集
                from markdown_import.client import fetch_ai_templates, inspect_markdown

            previews = {}
            for path in paths:
                try:
                    previews[path] = inspect_markdown(path, self.root_dir, allow_external_images=False)
                except Exception as exc:
                    previews[path] = {
                        "status": "error",
                        "title": path.stem,
                        "intro": "",
                        "date": "",
                        "tags": [],
                        "block_count": 0,
                        "block_media_count": 0,
                        "inline_image_count": 0,
                        "inline_images": [],
                        "external_images": [],
                        "external_images_allowed": False,
                        "local_files": [],
                        "errors": [{"source": path.name, "code": str(exc)}],
                    }
            duplicates = []
            try:
                import requests

                response = requests.post(
                    self.site_url.rstrip("/") + "/blog/api/markdown-import/duplicate-titles/",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={
                        "target_parent_id": self.target_parent_id,
                        "titles": [preview.get("title", path.stem) for path, preview in previews.items()],
                    },
                    timeout=15,
                )
                if response.status_code < 400:
                    duplicates = response.json().get("duplicates", [])
            except (OSError, ValueError, TypeError):
                # 同标题提示是增强信息，接口暂时不可用不阻断本地预检。
                duplicates = []
            templates = []
            template_error = ""
            try:
                templates = fetch_ai_templates(
                    self.site_url,
                    self.token,
                    self.target_parent_id,
                )
            except Exception as exc:
                # AI 建议是可选增强；模板接口不可用不能阻断手工填写和导入。
                template_error = safe_import_error(exc)
            return {
                "previews": previews,
                "duplicates": duplicates,
                "templates": templates,
                "template_error": template_error,
            }

        def success(value):
            previews = value["previews"]
            self.previews = previews
            self.duplicate_titles = value.get("duplicates", [])
            self.ai_templates = value.get("templates", [])
            self.ai_template_labels, self.ai_template_ids = ai_template_options(self.ai_templates)
            self.ai_template_descriptions = {
                int(item["id"]): str(item.get("description") or "").strip()
                for item in self.ai_templates
                if isinstance(item, dict) and str(item.get("id", "")).isdigit()
            }
            self.ai_template_error = value.get("template_error", "")
            self.ai_template_select.configure(values=self.ai_template_labels)
            self.file_metadata = {
                path: {
                    "title": preview.get("title", path.stem),
                    "intro": preview.get("intro", ""),
                    "date": preview.get("date", ""),
                    "tags": list(preview.get("tags", [])),
                    "ai_enabled": False,
                    "ai_template_id": None,
                    "ai_status": "",
                }
                for path, preview in previews.items()
            }
            self.preview = previews[paths[0]]
            self.idempotency_key = None
            self.external_images_var.set(False)
            for item in self.metadata_tree.get_children():
                self.metadata_tree.delete(item)
            for index, path in enumerate(paths):
                self._refresh_metadata_row(path, iid=str(index))
            self.metadata_tree.selection_set("0")
            self.metadata_tree.focus("0")
            self._metadata_selected()
            error_count = sum(bool(preview.get("errors")) for preview in previews.values())
            self.preview_status_var.set(f"已预检 {len(paths)} 个文件；{error_count} 个文件存在错误")
            self._refresh_import_state()

        self._run_async(worker, success)

    def _ai_option_changed(self, _event=None):
        self._metadata_field_changed()
        self._refresh_ai_controls()

    def _refresh_ai_controls(self):
        enabled = bool(self.ai_consent_var.get())
        has_templates = bool(self.ai_template_labels)
        selected = self.ai_template_ids.get(self.ai_template_var.get())
        self.ai_template_select.configure(
            state="readonly" if enabled and has_templates and not self.ai_busy else "disabled"
        )
        can_generate = enabled and selected is not None and self.active_metadata_path is not None and not self.ai_busy
        self.ai_generate_button.configure(state="normal" if can_generate else "disabled")
        if self.ai_template_error and enabled:
            self.ai_status_var.set(f"提示词模板暂不可用：{self.ai_template_error}")
        elif enabled and not has_templates:
            self.ai_status_var.set("网站当前没有已启用的提示词模板。")
        elif enabled and selected is None:
            self.ai_status_var.set("请选择当前文件要使用的提示词模板。")
        elif enabled and selected is not None:
            values = self.file_metadata.get(self.active_metadata_path, {})
            if not values.get("ai_status"):
                description = self.ai_template_descriptions.get(selected, "")
                self.ai_status_var.set(description or "已选择提示词模板，可以生成建议。")

    def generate_current_ai(self):
        self._metadata_field_changed()
        path = self.active_metadata_path
        template_id = self.ai_template_ids.get(self.ai_template_var.get())
        if path is None or not self.ai_consent_var.get() or template_id is None:
            self._show_error("请先允许 AI 请求并选择当前文件的提示词模板")
            return
        self.ai_busy = True
        self.ai_status_var.set("正在生成当前文件的简介和标签建议……")
        self.file_metadata[path]["ai_status"] = "正在生成建议"
        self._refresh_ai_controls()

        def worker():
            try:
                from tools.markdown_import.client import generate_ai_metadata
            except ModuleNotFoundError:  # PyInstaller 将 tools/markdown_import 作为顶层包收集
                from markdown_import.client import generate_ai_metadata
            return generate_ai_metadata(
                path,
                url=self.site_url,
                token=self.token,
                target_parent_id=self.target_parent_id,
                template_id=template_id,
            )

        def success(suggestion):
            self.ai_busy = False
            current = self.file_metadata.get(path, {})
            if not current.get("ai_enabled") or current.get("ai_template_id") != template_id:
                current["ai_status"] = "模板选择已变化，本次建议未应用。"
                if self.active_metadata_path == path:
                    self.ai_status_var.set(current["ai_status"])
                self._refresh_ai_controls()
                return
            values = apply_ai_suggestion(self.file_metadata.get(path, {}), suggestion)
            values["ai_status"] = "建议已生成，请检查后再导入。"
            self.file_metadata[path] = values
            if self.active_metadata_path == path:
                self._loading_metadata = True
                try:
                    self.intro_var.set(values["intro"])
                    self.tags_var.set(", ".join(values["tags"]))
                    self.ai_status_var.set(values["ai_status"])
                finally:
                    self._loading_metadata = False
            self._refresh_metadata_row(path)
            self._refresh_import_state()
            self._refresh_ai_controls()

        def failure(error):
            self.ai_busy = False
            code = safe_import_error(error)
            self.file_metadata[path]["ai_status"] = f"生成失败：{code}"
            if self.active_metadata_path == path:
                self.ai_status_var.set(self.file_metadata[path]["ai_status"])
            self._refresh_ai_controls()

        self._run_async(worker, success, failure)

    def _refresh_import_state(self):
        if not self.previews:
            return
        self._metadata_field_changed()
        ready = all(
            bool(self.file_metadata.get(path, {}).get("intro", "").strip())
            and not preview.get("errors")
            and (not preview.get("external_images") or self.external_images_var.get())
            for path, preview in self.previews.items()
        )
        self.import_button.configure(state="normal" if ready else "disabled")

    def start_import(self):
        if not self.selected_paths or not self.previews or not self.target_parent_id:
            return
        self._metadata_field_changed()
        invalid = [
            path.name
            for path in self.selected_paths
            if not self.file_metadata.get(path, {}).get("intro", "").strip()
            or self.previews.get(path, {}).get("errors")
        ]
        if invalid:
            self._show_error("请先修正这些文件的简介或预检错误：" + "、".join(invalid))
            return
        try:
            from tools.markdown_import.client import load_session_checkpoint
        except ModuleNotFoundError:  # PyInstaller 将 tools/markdown_import 作为顶层包收集
            from markdown_import.client import load_session_checkpoint

        active_checkpoint_paths = []
        terminal_checkpoint_paths = []
        for path in self.selected_paths:
            checkpoint = load_session_checkpoint(path, self.target_parent_id)
            if checkpoint_reuse_candidate(checkpoint):
                active_checkpoint_paths.append(path)
            elif checkpoint:
                # 完成、失败、过期和旧版 checkpoint 都不能再次占用旧幂等键。
                terminal_checkpoint_paths.append(path)

        force_new_paths = set(terminal_checkpoint_paths)
        if active_checkpoint_paths:
            names = "、".join(path.name for path in active_checkpoint_paths)
            resume = messagebox.askyesno(
                "发现未完成导入",
                f"以下文件存在未完成导入会话：{names}\n\n"
                "选择“是”尝试断点续传；只有标题、简介、标签、正文和媒体清单完全一致时才会继续，"
                "否则系统会自动生成新的幂等键。选择“否”将为这些文件新建导入。",
            )
            if not resume:
                force_new_paths.update(active_checkpoint_paths)
        if terminal_checkpoint_paths:
            names = "、".join(path.name for path in terminal_checkpoint_paths)
            messagebox.showinfo(
                "导入记录已结束",
                f"以下文件存在已完成、失败、过期或旧版导入记录：{names}\n\n"
                "本次不会复用旧幂等键，将创建新的导入会话。已有页面不会被自动删除或覆盖。",
            )

        if not messagebox.askyesno(
            "确认导入",
            f"将在索引页下创建 {len(self.selected_paths)} 个未发布 BlogPage 草稿。\n\n"
            "同标题页面不会阻止导入，Wagtail 会自动生成唯一 slug；已有页面不会自动删除或覆盖。\n\n"
            "继续吗？",
        ):
            return
        if not self.idempotency_key:
            self.idempotency_key = str(uuid.uuid4())
        self.import_button.configure(state="disabled")
        metadata = {
            path: {
                "title": values.get("title", "").strip(),
                "intro": values.get("intro", "").strip(),
                "date": values.get("date", "").strip(),
                "tags": [item.strip() for item in values.get("tags", []) if item.strip()],
            }
            for path, values in self.file_metadata.items()
        }
        allow_external = self.external_images_var.get()
        paths = list(self.selected_paths)
        parent_id = self.target_parent_id
        self.preview_status_var.set("正在上传媒体并创建未发布草稿……")

        def progress(stage, completed, total, payload):
            # Tk 控件只能在主线程更新；回调不显示令牌或本地绝对路径。
            labels = {
                "session_created": "已创建上传会话",
                "uploading": "正在上传媒体",
                "assembling": "媒体已上传，等待草稿组装",
            }
            if stage == "assembling":
                status = str(payload.get("status") or "")
                if status == "ready":
                    labels[stage] = "组装任务已提交，等待维护 Worker"
                elif status == "assembling":
                    labels[stage] = "正在组装未发布草稿"
            self.after(
                0,
                lambda: self.preview_status_var.set(
                    f"{labels.get(stage, '正在导入')}（媒体 {completed}/{total}）"
                ),
            )

        def worker():
            try:
                from tools.markdown_import.client import import_markdown
            except ModuleNotFoundError:  # PyInstaller 将 tools/markdown_import 作为顶层包收集
                from markdown_import.client import import_markdown

            results = []
            for index, path in enumerate(paths, start=1):
                try:
                    results.append(import_markdown(
                        path,
                        self.root_dir,
                        url=self.site_url,
                        token=self.token,
                        target_parent_id=parent_id,
                        idempotency_key=str(uuid.uuid4()),
                        allow_external_images=allow_external,
                        metadata_overrides=metadata[path],
                        force_new=path in force_new_paths,
                        progress_callback=progress,
                    ))
                except Exception as exc:
                    # 单文件失败只记录本文件，不能阻断同一批次的其他文章。
                    results.append({"status": "failed", "error": safe_import_error(exc)})
            return {"status": "batch", "results": results}

        def success(result):
            self._show_result(result)
            self.show_page("result")

        self._run_async(worker, success, lambda error: (self.import_button.configure(state="normal"), self._show_error(error)))

    def _show_result(self, result):
        if result.get("status") == "batch":
            self._set_text(self.result_text, "\n\n".join(
                f"文件 {self.selected_paths[index - 1].name if index <= len(self.selected_paths) else index}: "
                f"{item.get('status', 'unknown')}，页面 {item.get('page_id', '-')}，批次 {item.get('batch_id', '-')}"
                + (f"，错误 {item['error']}" if item.get("error") else "")
                for index, item in enumerate(result.get("results", []), start=1)
            ))
            page_id = next((item.get("page_id") for item in result.get("results", []) if item.get("page_id")), None)
            self.admin_url = admin_edit_url(self.site_url, page_id) if page_id else ""
            self.open_admin_button.configure(state="normal" if self.admin_url else "disabled")
            return
        status = result.get("status", "unknown")
        lines = [f"状态：{status}", f"批次 ID：{result.get('batch_id', '-')}", f"页面 ID：{result.get('page_id', '-')}", f"Revision ID：{result.get('revision_id', '-')}"]
        missing = result.get("missing") or []
        missing_details = result.get("missing_details") or []
        lines.append(f"缺失媒体：{len(missing)} 个")
        locations = {
            item["occurrence_id"]: format_inline_image_location(item)
            for item in (self.preview or {}).get("inline_images", [])
        }
        if missing_details:
            for item in missing_details:
                position_text = "、".join(
                    locations[occurrence_id]
                    for occurrence_id in item.get("occurrence_ids", [])
                    if occurrence_id in locations
                )
                suffix = f"（{position_text}）" if position_text else ""
                lines.append(
                    f"- {item.get('source', '未知媒体')}{suffix}："
                    f"{item.get('error_code', 'media_import_failed')}"
                )
        elif missing:
            lines.append("\n".join(f"- {item}" for item in missing))
        self._set_text(self.result_text, "\n".join(lines))
        page_id = result.get("page_id")
        self.admin_url = admin_edit_url(self.site_url, page_id) if page_id else ""
        self.open_admin_button.configure(state="normal" if self.admin_url else "disabled")

    def open_admin(self):
        if getattr(self, "admin_url", ""):
            webbrowser.open(self.admin_url)

    def _metadata_selected(self, _event=None):
        selected = self.metadata_tree.selection()
        if not selected:
            return
        index = int(selected[0])
        if index >= len(self.selected_paths):
            return
        self._metadata_field_changed()
        path = self.selected_paths[index]
        self.active_metadata_path = path
        values = self.file_metadata.get(path, {})
        self._loading_metadata = True
        try:
            self.title_var.set(values.get("title", path.stem))
            self.intro_var.set(values.get("intro", ""))
            self.date_var.set(values.get("date", ""))
            self.tags_var.set(", ".join(values.get("tags", [])))
            self.ai_consent_var.set(bool(values.get("ai_enabled")))
            selected_label = next(
                (label for label, template_id in self.ai_template_ids.items() if template_id == values.get("ai_template_id")),
                "",
            )
            self.ai_template_var.set(selected_label)
            self.ai_status_var.set(values.get("ai_status", ""))
        finally:
            self._loading_metadata = False
        self._refresh_ai_controls()
        preview = self.previews.get(path, {})
        details = [
            f"文件：{path.name}",
            f"服务端图片大小限制：{self.limits.get('max_image_size') or '未设置'}",
            f"音视频深度探测：{'可用' if self.limits.get('media_deep_probe') else '不可用（服务端将按失败关闭处理）'}",
            f"内容块：{preview.get('block_count', 0)}；独立媒体：{preview.get('block_media_count', 0)}；表格内图片：{preview.get('inline_image_count', 0)}",
        ]
        if preview.get("errors"):
            details.append("本地媒体错误：")
            details.extend(f"- {item['source']}：{item['code']}" for item in preview["errors"])
        else:
            details.append("本地媒体路径检查：通过")
        if preview.get("external_images"):
            details.append("远程图片：默认不下载；勾选确认后才会下载并上传")
        duplicates = [
            item for item in self.duplicate_titles
            if item.get("title") == self.file_metadata.get(path, {}).get("title")
        ]
        if duplicates:
            details.append(
                "同标题提示：索引页下已有 "
                + "、".join(
                    f"页面 {item.get('page_id')}（{'已发布' if item.get('live') else '未发布'}）"
                    for item in duplicates
                )
                + "；本次仍会创建新草稿，slug 将由 Wagtail 自动保证唯一。"
            )
        self._set_text(self.preview_details, "\n".join(details))

    def _metadata_field_changed(self, *_args):
        if self._loading_metadata or self.active_metadata_path is None:
            return
        values = self.file_metadata.setdefault(self.active_metadata_path, {})
        previous_template_id = values.get("ai_template_id")
        selected_template_id = self.ai_template_ids.get(self.ai_template_var.get())
        values.update(
            {
                "title": self.title_var.get(),
                "intro": self.intro_var.get(),
                "date": self.date_var.get(),
                "tags": [item.strip() for item in self.tags_var.get().split(",") if item.strip()],
                "ai_enabled": bool(self.ai_consent_var.get()),
                "ai_template_id": selected_template_id,
            }
        )
        if previous_template_id != selected_template_id:
            values["ai_status"] = ""
        self._refresh_metadata_row(self.active_metadata_path)

    def _refresh_metadata_row(self, path: Path, iid: str | None = None):
        if iid is None:
            try:
                iid = str(self.selected_paths.index(path))
            except ValueError:
                return
        values = self.file_metadata.get(path, {})
        preview = self.previews.get(path, {})
        status = "有错误" if preview.get("errors") else ("待填写简介" if not values.get("intro", "").strip() else "可导入")
        row = (path.name, values.get("title", ""), ", ".join(values.get("tags", [])), status)
        if self.metadata_tree.exists(iid):
            self.metadata_tree.item(iid, values=row)
        else:
            self.metadata_tree.insert("", "end", iid=iid, values=row)

    @staticmethod
    def _set_text(widget, value: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

def main():
    app = MarkdownImportApp()
    app.mainloop()


if __name__ == "__main__":
    main()
