from __future__ import annotations

import json
import os
import queue
import base64
import ctypes
import struct
import subprocess
import sys
import threading
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    VERTICAL,
    BooleanVar,
    Canvas,
    Listbox,
    Menu,
    PhotoImage,
    StringVar,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
    ttk,
)


APP_NAME = "SeekOpen"
APP_VERSION = "1.1.0"
NO_EXTENSION = "<no-extension>"
MAX_RECENT_FILES = 50
DEFAULT_IGNORED_EXTENSIONS = [".o", ".obj", ".d", ".dep", ".pyc", ".tmp"]
DEFAULT_IGNORED_DIRECTORIES = [
    ".git",
    ".svn",
    ".idea",
    ".vs",
    "__pycache__",
    "node_modules",
]


def normalize_extension(value: str) -> str:
    value = value.strip().lower()
    if not value:
        return ""
    if value.startswith("*"):
        value = value[1:]
    return value if value.startswith(".") else f".{value}"


def normalize_extensions(values: list[str]) -> list[str]:
    return sorted({ext for value in values if (ext := normalize_extension(value))})


def config_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home()))
    return base / APP_NAME / "settings.json"


def resource_path(*parts: str) -> Path:
    """同时兼容源码运行和 PyInstaller 打包后的资源目录。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def path_identity(value: str | Path) -> str:
    """返回适合去重的路径标识；不要求目标当前存在。"""
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))


def unique_path_strings(values: list[str | Path]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        identity = path_identity(text)
        if identity not in seen:
            seen.add(identity)
            result.append(str(Path(text)))
    return result


def add_favorite_paths(existing: list[str], additions: list[str | Path]) -> list[str]:
    return unique_path_strings(existing + additions)


def add_recent_file(existing: list[str], path: str | Path, limit: int = MAX_RECENT_FILES) -> list[str]:
    identity = path_identity(path)
    remaining = [value for value in existing if path_identity(value) != identity]
    return unique_path_strings([path] + remaining)[:limit]


def remove_path_records(existing: list[str], removals: list[str | Path]) -> list[str]:
    identities = {path_identity(value) for value in removals}
    return [value for value in existing if path_identity(value) not in identities]


@dataclass
class AppConfig:
    last_project: str = ""
    recent_projects: list[str] = field(default_factory=list)
    selected_file_types: list[str] = field(
        default_factory=lambda: DEFAULT_IGNORED_EXTENSIONS.copy()
    )
    file_type_mode: str = "ignore"
    filter_enabled: bool = True
    ignored_directories: list[str] = field(
        default_factory=lambda: DEFAULT_IGNORED_DIRECTORIES.copy()
    )
    favorite_paths: list[str] = field(default_factory=list)
    recent_files: list[str] = field(default_factory=list)
    track_recent_files: bool = True
    last_view: str = "project"
    window_geometry: str = "1180x760"

    @classmethod
    def load(cls) -> "AppConfig":
        path = config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                last_project=str(data.get("last_project", "")),
                recent_projects=[str(x) for x in data.get("recent_projects", [])],
                selected_file_types=sorted(
                    {
                        str(value) if str(value) == NO_EXTENSION else normalize_extension(str(value))
                        for value in data.get(
                            "selected_file_types",
                            data.get("ignored_extensions", DEFAULT_IGNORED_EXTENSIONS),
                        )
                        if str(value).strip()
                    }
                ),
                file_type_mode=(
                    str(data.get("file_type_mode", "ignore"))
                    if str(data.get("file_type_mode", "ignore")) in {"ignore", "show"}
                    else "ignore"
                ),
                filter_enabled=bool(data.get("filter_enabled", True)),
                ignored_directories=sorted(
                    {str(x).strip() for x in data.get("ignored_directories", DEFAULT_IGNORED_DIRECTORIES) if str(x).strip()}
                ),
                favorite_paths=unique_path_strings(
                    [str(value) for value in data.get("favorite_paths", [])]
                ),
                recent_files=unique_path_strings(
                    [str(value) for value in data.get("recent_files", [])]
                )[:MAX_RECENT_FILES],
                track_recent_files=bool(data.get("track_recent_files", True)),
                last_view=(
                    str(data.get("last_view", "project"))
                    if str(data.get("last_view", "project")) in {"project", "favorites", "recent"}
                    else "project"
                ),
                window_geometry=str(data.get("window_geometry", "1180x760")),
            )
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        path = config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.__dict__, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


@dataclass(frozen=True)
class FileRecord:
    path: Path
    relative: Path
    is_dir: bool


def file_type_key(path: Path) -> str:
    return path.suffix.lower() or NO_EXTENSION


def filter_records_by_type(
    records: list[FileRecord], mode: str, selected_types: set[str]
) -> list[FileRecord]:
    result: list[FileRecord] = []
    for record in records:
        if record.is_dir:
            result.append(record)
            continue
        selected = file_type_key(record.path) in selected_types
        if (mode == "show" and selected) or (mode != "show" and not selected):
            result.append(record)
    return result


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def rgba_to_png(width: int, height: int, rgba: bytes) -> bytes:
    rows = b"".join(b"\0" + rgba[y * width * 4 : (y + 1) * width * 4] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(rows, 9))
        + _png_chunk(b"IEND", b"")
    )


class _SHFILEINFO(ctypes.Structure):
    _fields_ = [
        ("hIcon", ctypes.c_void_p),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", ctypes.c_ulong),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_ulong),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_ulong),
        ("biSizeImage", ctypes.c_ulong),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_ulong),
        ("biClrImportant", ctypes.c_ulong),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_ulong * 3)]


class WindowsShellIcons:
    """从 Windows Shell 读取文件关联图标，并转换成 Tk 可用的 PNG。"""

    SHGFI_ICON = 0x000000100
    SHGFI_TYPENAME = 0x000000400
    SHGFI_USEFILEATTRIBUTES = 0x000000010
    SHGFI_SMALLICON = 0x000000001
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_NORMAL = 0x80
    DI_NORMAL = 0x0003

    def __init__(self) -> None:
        self.available = os.name == "nt"
        self._png_cache: dict[str, bytes | None] = {}
        self._type_cache: dict[str, str] = {}
        if self.available:
            self.shell32 = ctypes.windll.shell32
            self.user32 = ctypes.windll.user32
            self.gdi32 = ctypes.windll.gdi32
            self.shell32.SHGetFileInfoW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_ulong,
                ctypes.POINTER(_SHFILEINFO),
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            self.shell32.SHGetFileInfoW.restype = ctypes.c_void_p
            self.user32.GetDC.argtypes = [ctypes.c_void_p]
            self.user32.GetDC.restype = ctypes.c_void_p
            self.user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self.user32.DestroyIcon.argtypes = [ctypes.c_void_p]
            self.user32.DrawIconEx.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_uint,
            ]
            self.gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
            self.gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
            self.gdi32.CreateDIBSection.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_BITMAPINFO),
                ctypes.c_uint,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_void_p,
                ctypes.c_ulong,
            ]
            self.gdi32.CreateDIBSection.restype = ctypes.c_void_p
            self.gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self.gdi32.SelectObject.restype = ctypes.c_void_p
            self.gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
            self.gdi32.DeleteDC.argtypes = [ctypes.c_void_p]

    @staticmethod
    def display_extension(type_key: str) -> str:
        return "（无扩展名）" if type_key == NO_EXTENSION else type_key

    def _query(self, type_key: str, flags: int) -> _SHFILEINFO | None:
        if not self.available:
            return None
        info = _SHFILEINFO()
        is_directory = type_key == "<folder>"
        name = "folder" if is_directory else ("file" if type_key == NO_EXTENSION else f"file{type_key}")
        attributes = self.FILE_ATTRIBUTE_DIRECTORY if is_directory else self.FILE_ATTRIBUTE_NORMAL
        result = self.shell32.SHGetFileInfoW(
            name,
            attributes,
            ctypes.byref(info),
            ctypes.sizeof(info),
            flags | self.SHGFI_USEFILEATTRIBUTES,
        )
        return info if result else None

    def type_name(self, type_key: str) -> str:
        if type_key == "<folder>":
            return "文件夹"
        if type_key in self._type_cache:
            return self._type_cache[type_key]
        info = self._query(type_key, self.SHGFI_TYPENAME)
        fallback = "文件" if type_key == NO_EXTENSION else f"{type_key[1:].upper()} 文件"
        value = info.szTypeName if info and info.szTypeName else fallback
        self._type_cache[type_key] = value
        return value

    def icon_png(self, type_key: str, size: int = 16) -> bytes | None:
        if type_key in self._png_cache:
            return self._png_cache[type_key]
        info = self._query(type_key, self.SHGFI_ICON | self.SHGFI_SMALLICON)
        if not info or not info.hIcon:
            self._png_cache[type_key] = None
            return None
        try:
            png = self._hicon_to_png(info.hIcon, size)
            self._png_cache[type_key] = png
            return png
        finally:
            self.user32.DestroyIcon(info.hIcon)

    def _hicon_to_png(self, icon: int, size: int) -> bytes | None:
        screen_dc = self.user32.GetDC(0)
        memory_dc = self.gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        bitmap_info = _BITMAPINFO()
        bitmap_info.bmiHeader = _BITMAPINFOHEADER(
            ctypes.sizeof(_BITMAPINFOHEADER), size, -size, 1, 32, 0, size * size * 4, 0, 0, 0, 0
        )
        bitmap = self.gdi32.CreateDIBSection(memory_dc, ctypes.byref(bitmap_info), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            self.gdi32.DeleteDC(memory_dc)
            self.user32.ReleaseDC(0, screen_dc)
            return None
        previous = self.gdi32.SelectObject(memory_dc, bitmap)
        ctypes.memset(bits, 0, size * size * 4)
        self.user32.DrawIconEx(memory_dc, 0, 0, icon, size, size, 0, None, self.DI_NORMAL)
        bgra = ctypes.string_at(bits, size * size * 4)
        self.gdi32.SelectObject(memory_dc, previous)
        self.gdi32.DeleteObject(bitmap)
        self.gdi32.DeleteDC(memory_dc)
        self.user32.ReleaseDC(0, screen_dc)
        rgba = bytearray(size * size * 4)
        has_alpha = any(bgra[index + 3] for index in range(0, len(bgra), 4))
        for index in range(0, len(bgra), 4):
            blue, green, red, alpha = bgra[index : index + 4]
            if not has_alpha:
                alpha = 255 if (red or green or blue) else 0
            rgba[index : index + 4] = bytes((red, green, blue, alpha))
        return rgba_to_png(size, size, bytes(rgba))


def scan_project(
    root: Path,
    ignored_extensions: set[str],
    ignored_directories: set[str],
    stop_event: threading.Event,
) -> list[FileRecord]:
    records: list[FileRecord] = []
    ignored_dir_names = {name.casefold() for name in ignored_directories}

    def on_error(_: OSError) -> None:
        return

    for current, dirs, files in os.walk(root, topdown=True, onerror=on_error):
        if stop_event.is_set():
            return []
        dirs[:] = sorted(
            (d for d in dirs if d.casefold() not in ignored_dir_names),
            key=str.casefold,
        )
        current_path = Path(current)
        for directory in dirs:
            path = current_path / directory
            records.append(FileRecord(path, path.relative_to(root), True))
        for filename in sorted(files, key=str.casefold):
            path = current_path / filename
            if path.suffix.lower() in ignored_extensions:
                continue
            records.append(FileRecord(path, path.relative_to(root), False))
    return records


class FileTypeDialog(Toplevel):
    def __init__(self, parent: "SeekOpenApp") -> None:
        super().__init__(parent)
        self.parent = parent
        self.title("文件类型与忽略规则")
        self.geometry("720x660")
        self.minsize(600, 520)
        self.configure(background="#F4F7FB")
        self.transient(parent)
        self.grab_set()

        body = ttk.Frame(self, padding=16)
        body.pack(fill=BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        ttk.Label(body, text="当前工程中的文件类型", font=("Microsoft YaHei UI", 11, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        mode_row = ttk.Frame(body)
        mode_row.grid(row=1, column=0, sticky="ew")
        self.enabled_var = BooleanVar(value=parent.settings.filter_enabled)
        self.mode_var = StringVar(value=parent.settings.file_type_mode)
        ttk.Checkbutton(
            mode_row,
            text="启用文件类型筛选",
            variable=self.enabled_var,
            style="Switch.TCheckbutton",
        ).pack(side=LEFT, padx=(0, 22))
        ttk.Radiobutton(mode_row, text="忽略选中的类型", value="ignore", variable=self.mode_var).pack(side=LEFT)
        ttk.Radiobutton(mode_row, text="只显示选中的类型", value="show", variable=self.mode_var).pack(side=LEFT, padx=(22, 0))

        actions = ttk.Frame(body)
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 5))
        ttk.Label(actions, text="勾选项会按上面的模式生效；类型来自当前工程扫描结果。").pack(side=LEFT)
        ttk.Button(actions, text="全选", command=lambda: self._set_types(True)).pack(side=RIGHT)
        ttk.Button(actions, text="全不选", command=lambda: self._set_types(False)).pack(side=RIGHT, padx=5)
        ttk.Button(actions, text="反选", command=self._invert_types).pack(side=RIGHT)

        type_box = ttk.Frame(body, relief="sunken", borderwidth=1)
        type_box.grid(row=4, column=0, sticky="nsew")
        type_box.columnconfigure(0, weight=1)
        type_box.rowconfigure(0, weight=1)
        canvas = Canvas(type_box, highlightthickness=0, background="white")
        scrollbar = ttk.Scrollbar(type_box, orient=VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        type_frame = ttk.Frame(canvas, padding=8)
        window_id = canvas.create_window((0, 0), window=type_frame, anchor="nw")
        type_frame.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

        counts = parent.available_file_types()
        selected = set(parent.settings.selected_file_types)
        self.type_vars: dict[str, BooleanVar] = {}
        if counts:
            for index, type_key in enumerate(sorted(counts, key=lambda value: (value == NO_EXTENSION, value))):
                var = BooleanVar(value=type_key in selected)
                self.type_vars[type_key] = var
                extension = parent.shell_icons.display_extension(type_key)
                description = parent.shell_icons.type_name(type_key)
                label = f"{extension}   {description}   ({counts[type_key]})"
                ttk.Checkbutton(type_frame, text=label, variable=var).grid(
                    row=index // 2, column=index % 2, sticky="w", padx=(4, 24), pady=3
                )
            type_frame.columnconfigure(0, weight=1)
            type_frame.columnconfigure(1, weight=1)
        else:
            ttk.Label(type_frame, text="工程中还没有扫描到文件，请等待扫描完成后再打开此窗口。").grid(
                row=0, column=0, sticky="w", padx=4, pady=8
            )

        ttk.Separator(body).grid(row=5, column=0, sticky="ew", pady=12)
        ttk.Label(body, text="忽略文件夹名称", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=6, column=0, sticky="w", pady=(0, 4)
        )
        directory_row = ttk.Frame(body)
        directory_row.grid(row=7, column=0, sticky="ew")
        directory_row.columnconfigure(0, weight=1)
        self.directory_list = Listbox(directory_row, height=4, selectmode="extended")
        self.directory_list.grid(row=0, column=0, rowspan=2, sticky="nsew")
        for name in parent.settings.ignored_directories:
            self.directory_list.insert(END, name)
        self.new_directory_var = StringVar()
        entry = ttk.Entry(directory_row, textvariable=self.new_directory_var, width=22)
        entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        entry.bind("<Return>", lambda _: self._add_directory())
        directory_buttons = ttk.Frame(directory_row)
        directory_buttons.grid(row=1, column=1, sticky="nw", padx=(8, 0), pady=(5, 0))
        ttk.Button(directory_buttons, text="添加", command=self._add_directory).pack(side=LEFT)
        ttk.Button(directory_buttons, text="删除选中", command=self._remove_directory).pack(side=LEFT, padx=(5, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side=RIGHT)
        ttk.Button(buttons, text="保存并刷新", command=self._save).pack(side=RIGHT, padx=(0, 8))
        self.bind("<Escape>", lambda _: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.focus_set()

    def _set_types(self, selected: bool) -> None:
        for var in self.type_vars.values():
            var.set(selected)

    def _invert_types(self) -> None:
        for var in self.type_vars.values():
            var.set(not var.get())

    def _add_directory(self) -> None:
        value = self.new_directory_var.get().strip()
        if value and value not in self.directory_list.get(0, END):
            self.directory_list.insert(END, value)
        self.new_directory_var.set("")

    def _remove_directory(self) -> None:
        for index in reversed(self.directory_list.curselection()):
            self.directory_list.delete(index)

    def _save(self) -> None:
        self.parent.settings.selected_file_types = sorted(
            type_key for type_key, var in self.type_vars.items() if var.get()
        )
        self.parent.settings.file_type_mode = self.mode_var.get()
        self.parent.settings.filter_enabled = self.enabled_var.get()
        self.parent.settings.ignored_directories = sorted(
            {str(x).strip() for x in self.directory_list.get(0, END) if str(x).strip()}
        )
        self.parent.settings.save()
        self.destroy()
        self.parent._update_filter_summary()
        self.parent.refresh_project()


class SeekOpenApp(Tk):
    def __init__(self) -> None:
        super().__init__()
        self.settings = AppConfig.load()
        self.title(f"{APP_NAME} {APP_VERSION} - 工程速开")
        self.geometry(self.settings.window_geometry)
        self.minsize(900, 600)
        self.option_add("*Font", ("Microsoft YaHei UI", 9))
        self.configure(background="#F4F7FB")

        self.brand_icon: PhotoImage | None = None
        try:
            icon_file = resource_path("assets", "seekopen-icon-64.png")
            if icon_file.is_file():
                self.brand_icon = PhotoImage(file=str(icon_file))
                self.iconphoto(True, self.brand_icon)
            ico_file = resource_path("assets", "seekopen.ico")
            if ico_file.is_file():
                self.iconbitmap(str(ico_file))
        except Exception:
            self.brand_icon = None

        self.shell_icons = WindowsShellIcons()
        self.tk_icons: dict[str, PhotoImage] = {}
        self.project_root: Path | None = None
        self.all_records: list[FileRecord] = []
        self.item_paths: dict[str, Path] = {}
        self.scan_generation = 0
        self.stop_event = threading.Event()
        self.result_queue: queue.Queue[tuple[int, list[FileRecord]]] = queue.Queue()
        self.search_after_id: str | None = None

        self._build_ui()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_results)

        if self.settings.last_project and Path(self.settings.last_project).is_dir():
            self.open_project(Path(self.settings.last_project), switch_view=False)
        else:
            self._on_view_changed()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        colors = {
            "background": "#F4F7FB",
            "surface": "#FFFFFF",
            "border": "#DCE3EC",
            "text": "#172033",
            "muted": "#64748B",
            "accent": "#1769E0",
            "accent_hover": "#0F5BC7",
            "selection": "#DCEBFF",
        }
        style.configure("App.TFrame", background=colors["background"])
        style.configure("TFrame", background=colors["surface"])
        style.configure("TLabel", background=colors["surface"], foreground=colors["text"])
        style.configure("TCheckbutton", background=colors["surface"], foreground=colors["text"])
        style.configure("TRadiobutton", background=colors["surface"], foreground=colors["text"])
        style.configure("Header.TFrame", background=colors["surface"])
        style.configure("Card.TFrame", background=colors["surface"])
        style.configure("Footer.TFrame", background=colors["surface"])
        style.configure("Title.TLabel", background=colors["surface"], foreground=colors["text"], font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Subtitle.TLabel", background=colors["surface"], foreground=colors["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", background=colors["surface"], foreground=colors["muted"], font=("Microsoft YaHei UI", 8))
        style.configure("Project.TLabel", background=colors["surface"], foreground=colors["text"], font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", background=colors["surface"], foreground=colors["muted"])
        style.configure("TButton", padding=(11, 7), relief="flat", font=("Microsoft YaHei UI", 9))
        style.map("TButton", background=[("active", "#E8EEF7")])
        style.configure("Accent.TButton", background=colors["accent"], foreground="#FFFFFF", bordercolor=colors["accent"], focuscolor=colors["accent"], padding=(15, 8), font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", colors["accent_hover"]), ("pressed", "#0B4FAF")], foreground=[("disabled", "#D8E6FA")])
        style.configure("FilterOn.TButton", background="#EFF5FF", foreground="#1859B7", bordercolor="#BFD5F5")
        style.map("FilterOn.TButton", background=[("active", "#DDEBFF")])
        style.configure("FilterOff.TButton", background="#FFF4E5", foreground="#9A5700", bordercolor="#F1D09B")
        style.map("FilterOff.TButton", background=[("active", "#FFE8C2")])
        style.configure("ChipOn.TLabel", background="#E7F1FF", foreground="#155BB8", padding=(9, 5), font=("Microsoft YaHei UI", 8))
        style.configure("ChipOff.TLabel", background="#FFF1D9", foreground="#8A5200", padding=(9, 5), font=("Microsoft YaHei UI", 8))
        style.configure("Switch.TCheckbutton", background=colors["surface"], foreground=colors["text"])
        style.configure("Treeview", background=colors["surface"], fieldbackground=colors["surface"], foreground=colors["text"], borderwidth=0, relief="flat", rowheight=29, font=("Microsoft YaHei UI", 9))
        style.map("Treeview", background=[("selected", colors["selection"])], foreground=[("selected", colors["text"])])
        style.configure("Treeview.Heading", background="#EEF3F8", foreground="#42526A", relief="flat", padding=(9, 8), font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview.Heading", background=[("active", "#E2EAF3")])
        style.configure("TEntry", padding=(8, 7), fieldbackground="#FFFFFF", bordercolor=colors["border"], lightcolor=colors["border"], darkcolor=colors["border"])

        header = ttk.Frame(self, style="Header.TFrame", padding=(18, 12))
        header.pack(fill="x")
        if self.brand_icon:
            ttk.Label(header, image=self.brand_icon, style="Title.TLabel").pack(side=LEFT, padx=(0, 11))
        title_box = ttk.Frame(header, style="Header.TFrame")
        title_box.pack(side=LEFT)
        ttk.Label(title_box, text="SeekOpen", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="工程浏览 · 全局快捷访问 · 多文件速开", style="Subtitle.TLabel").pack(anchor="w", pady=(1, 0))
        ttk.Button(header, text="选择工程", command=self.choose_project, style="Accent.TButton").pack(side=RIGHT)
        ttk.Button(header, text="刷新  F5", command=self.refresh_project).pack(side=RIGHT, padx=(0, 8))

        ttk.Separator(self).pack(fill="x")
        main = ttk.Frame(self, style="App.TFrame", padding=(18, 14, 18, 12))
        main.pack(fill=BOTH, expand=True)

        project_card = ttk.Frame(main, style="Card.TFrame", padding=(14, 11))
        project_card.pack(fill="x", pady=(0, 12))
        project_top = ttk.Frame(project_card, style="Card.TFrame")
        project_top.pack(fill="x")
        ttk.Label(project_top, text="当前工程", style="Section.TLabel").pack(side=LEFT, padx=(0, 10))
        self.project_var = StringVar(value="未选择")
        ttk.Label(project_top, textvariable=self.project_var, style="Project.TLabel").pack(side=LEFT, fill="x", expand=True)
        self.recent_button = ttk.Menubutton(project_top, text="最近工程 ▾")
        self.recent_menu = Menu(self.recent_button, tearoff=False)
        self.recent_button["menu"] = self.recent_menu
        self.recent_button.pack(side=RIGHT)

        controls = ttk.Frame(project_card, style="Card.TFrame")
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(controls, text="文件类型与规则", command=self.open_file_type_dialog).pack(side=LEFT)
        self.filter_toggle_button = ttk.Button(controls, command=self.toggle_filter)
        self.filter_toggle_button.pack(side=LEFT, padx=(7, 0))
        self.filter_var = StringVar()
        self.filter_badge = ttk.Label(controls, textvariable=self.filter_var)
        self.filter_badge.pack(side=LEFT, padx=(8, 0))
        ttk.Separator(controls, orient=VERTICAL).pack(side=LEFT, fill="y", padx=12)
        ttk.Button(controls, text="展开全部", command=lambda: self._set_all_open(True)).pack(side=LEFT)
        ttk.Button(controls, text="折叠全部", command=lambda: self._set_all_open(False)).pack(side=LEFT, padx=(5, 0))

        search_box = ttk.Frame(controls, style="Card.TFrame")
        search_box.pack(side=RIGHT, fill="x", expand=True, padx=(24, 0))
        ttk.Label(search_box, text="搜索文件", style="Section.TLabel").pack(side=LEFT, padx=(0, 7))
        self.search_var = StringVar()
        search = ttk.Entry(search_box, textvariable=self.search_var, width=34)
        search.pack(side=RIGHT, fill="x", expand=True)
        search.bind("<KeyRelease>", self._schedule_filter)
        self.search_entry = search

        self._update_recent_menu()
        self._update_filter_summary()

        style.configure("TNotebook", background=colors["background"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(18, 9), font=("Microsoft YaHei UI", 9), background="#E8EEF6", foreground=colors["muted"])
        style.map(
            "TNotebook.Tab",
            background=[("selected", colors["surface"]), ("active", "#EFF4FA")],
            foreground=[("selected", colors["accent"])],
        )

        self.view_notebook = ttk.Notebook(main)
        self.view_notebook.pack(fill=BOTH, expand=True)

        project_tab = ttk.Frame(self.view_notebook, style="Card.TFrame", padding=1)
        project_tab.rowconfigure(0, weight=1)
        project_tab.columnconfigure(0, weight=1)
        self.view_notebook.add(project_tab, text="  当前工程  ")

        columns = ("type", "path")
        self.tree = ttk.Treeview(project_tab, columns=columns, selectmode="extended")
        self.tree.heading("#0", text="名称", anchor="w")
        self.tree.heading("type", text="类型", anchor="w")
        self.tree.heading("path", text="相对路径", anchor="w")
        self.tree.column("#0", width=300, minwidth=160)
        self.tree.column("type", width=165, minwidth=100, stretch=False)
        self.tree.column("path", width=520, minwidth=180)
        scrollbar = ttk.Scrollbar(project_tab, orient=VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>", lambda _: self.open_selected())
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<<TreeviewSelect>>", self._update_selection_status)

        favorites_tab = ttk.Frame(self.view_notebook, style="Card.TFrame", padding=(10, 9, 10, 10))
        favorites_tab.rowconfigure(1, weight=1)
        favorites_tab.columnconfigure(0, weight=1)
        self.view_notebook.add(favorites_tab, text="  ★ 快捷访问  ")
        favorites_toolbar = ttk.Frame(favorites_tab, style="Card.TFrame")
        favorites_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(favorites_toolbar, text="可收藏工程内外的任意文件或文件夹", style="Status.TLabel").pack(side=LEFT)
        ttk.Button(favorites_toolbar, text="添加文件", command=self.add_external_files).pack(side=RIGHT)
        ttk.Button(favorites_toolbar, text="添加文件夹", command=self.add_external_folder).pack(side=RIGHT, padx=(0, 6))
        ttk.Button(favorites_toolbar, text="移除选中 ×", command=self.remove_selected_records).pack(side=RIGHT, padx=(0, 6))
        self.favorite_tree = self._create_path_list_tree(favorites_tab)
        self.favorite_tree.grid(row=1, column=0, sticky="nsew")
        favorite_scrollbar = ttk.Scrollbar(favorites_tab, orient=VERTICAL, command=self.favorite_tree.yview)
        self.favorite_tree.configure(yscrollcommand=favorite_scrollbar.set)
        favorite_scrollbar.grid(row=1, column=1, sticky="ns")

        recent_tab = ttk.Frame(self.view_notebook, style="Card.TFrame", padding=(10, 9, 10, 10))
        recent_tab.rowconfigure(1, weight=1)
        recent_tab.columnconfigure(0, weight=1)
        self.view_notebook.add(recent_tab, text="  最近打开  ")
        recent_toolbar = ttk.Frame(recent_tab, style="Card.TFrame")
        recent_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.track_recent_var = BooleanVar(value=self.settings.track_recent_files)
        ttk.Checkbutton(
            recent_toolbar,
            text="记录最近打开的文件",
            variable=self.track_recent_var,
            command=self.toggle_recent_tracking,
        ).pack(side=LEFT)
        ttk.Button(recent_toolbar, text="清空记录", command=self.clear_recent_files).pack(side=RIGHT)
        ttk.Button(recent_toolbar, text="移除选中 ×", command=self.remove_selected_records).pack(side=RIGHT, padx=(0, 6))
        self.recent_tree = self._create_path_list_tree(recent_tab)
        self.recent_tree.grid(row=1, column=0, sticky="nsew")
        recent_scrollbar = ttk.Scrollbar(recent_tab, orient=VERTICAL, command=self.recent_tree.yview)
        self.recent_tree.configure(yscrollcommand=recent_scrollbar.set)
        recent_scrollbar.grid(row=1, column=1, sticky="ns")

        self.project_tab = project_tab
        self.favorites_tab = favorites_tab
        self.recent_tab = recent_tab
        self.favorite_item_paths: dict[str, Path] = {}
        self.recent_item_paths: dict[str, Path] = {}
        self._bind_path_list_tree(self.favorite_tree)
        self._bind_path_list_tree(self.recent_tree)
        self.favorite_tree.bind("<Button-1>", lambda event: self._handle_remove_column(event, "favorites"), add="+")
        self.recent_tree.bind("<Button-1>", lambda event: self._handle_remove_column(event, "recent"), add="+")
        self.view_notebook.bind("<<NotebookTabChanged>>", self._on_view_changed)
        self._populate_favorites()
        self._populate_recent_files()
        initial_tabs = {"project": project_tab, "favorites": favorites_tab, "recent": recent_tab}
        self.view_notebook.select(initial_tabs.get(self.settings.last_view, project_tab))

        bottom = ttk.Frame(self, style="Footer.TFrame", padding=(18, 9))
        bottom.pack(fill="x")
        self.status_var = StringVar()
        ttk.Label(bottom, textvariable=self.status_var, style="Status.TLabel").pack(side=LEFT, fill="x", expand=True)
        ttk.Label(bottom, text="双击或 Enter 打开", style="Status.TLabel").pack(side=RIGHT, padx=(0, 14))
        ttk.Button(bottom, text="打开选中项", command=self.open_selected, style="Accent.TButton").pack(side=RIGHT)

        self.context_menu = Menu(self, tearoff=False)
        self.context_menu.add_command(label="打开", command=self.open_selected)
        self.context_menu.add_command(label="用系统默认程序打开", command=self.open_selected)
        self.context_menu.add_command(label="固定到快捷访问", command=self.pin_selected)
        self.context_menu.add_command(label="从当前列表移除", command=self.remove_selected_records)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="设为当前工程", command=self.set_selected_as_project)
        self.context_menu.add_command(label="在资源管理器中显示", command=self.reveal_selected)
        self.context_menu.add_command(label="在此处打开 CMD", command=self.open_cmd_here)
        self.context_menu.add_command(label="在此处打开 PowerShell", command=self.open_powershell_here)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="运行 Python 脚本", command=self.run_python_selected)
        self.context_menu.add_command(label="运行 Python 脚本（窗口保留）", command=lambda: self.run_python_selected(True))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="复制完整路径", command=self.copy_paths)

    def _create_path_list_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        tree = ttk.Treeview(
            parent,
            columns=("type", "path", "remove"),
            selectmode="extended",
        )
        tree.heading("#0", text="名称", anchor="w")
        tree.heading("type", text="类型", anchor="w")
        tree.heading("path", text="完整路径", anchor="w")
        tree.heading("remove", text="", anchor="center")
        tree.column("#0", width=280, minwidth=150)
        tree.column("type", width=165, minwidth=100, stretch=False)
        tree.column("path", width=560, minwidth=220)
        tree.column("remove", width=42, minwidth=42, stretch=False, anchor="center")
        return tree

    def _bind_path_list_tree(self, tree: ttk.Treeview) -> None:
        tree.bind("<Double-1>", self._on_double_click)
        tree.bind("<Return>", lambda _: self.open_selected())
        tree.bind("<Delete>", lambda _: self.remove_selected_records())
        tree.bind("<Button-3>", self._show_context_menu)
        tree.bind("<<TreeviewSelect>>", self._update_selection_status)

    def _active_view(self) -> str:
        current = self.view_notebook.select()
        if current == str(self.favorites_tab):
            return "favorites"
        if current == str(self.recent_tab):
            return "recent"
        return "project"

    def _active_tree(self) -> ttk.Treeview:
        view = self._active_view()
        if view == "favorites":
            return self.favorite_tree
        if view == "recent":
            return self.recent_tree
        return self.tree

    def _path_map_for_tree(self, tree: ttk.Treeview) -> dict[str, Path]:
        if tree is self.favorite_tree:
            return self.favorite_item_paths
        if tree is self.recent_tree:
            return self.recent_item_paths
        return self.item_paths

    def _populate_path_list(
        self,
        tree: ttk.Treeview,
        item_paths: dict[str, Path],
        values: list[str],
    ) -> None:
        tree.delete(*tree.get_children())
        item_paths.clear()
        for value in values:
            path = Path(value)
            exists = path.exists()
            is_directory = path.is_dir() if exists else False
            type_key = "<folder>" if is_directory else file_type_key(path)
            type_name = self.shell_icons.type_name(type_key) if exists else "文件不存在"
            icon = self._icon_for(type_key) if exists else None
            item = tree.insert(
                "",
                END,
                text=path.name or str(path),
                values=(type_name, str(path), "×"),
                image=icon if icon else "",
                tags=("missing",) if not exists else (),
            )
            item_paths[item] = path
        tree.tag_configure("missing", foreground="#A36A6A")

    def _populate_favorites(self) -> None:
        self._populate_path_list(
            self.favorite_tree,
            self.favorite_item_paths,
            self.settings.favorite_paths,
        )
        self.view_notebook.tab(self.favorites_tab, text=f"  ★ 快捷访问 ({len(self.settings.favorite_paths)})  ")
        if self._active_view() == "favorites":
            self.status_var.set(f"快捷访问共 {len(self.settings.favorite_paths)} 项")

    def _populate_recent_files(self) -> None:
        self._populate_path_list(
            self.recent_tree,
            self.recent_item_paths,
            self.settings.recent_files,
        )
        self.view_notebook.tab(self.recent_tab, text=f"  最近打开 ({len(self.settings.recent_files)})  ")
        if self._active_view() == "recent":
            state = "已开启记录" if self.settings.track_recent_files else "已暂停记录"
            self.status_var.set(f"最近打开 {len(self.settings.recent_files)} 项 · {state}")

    def _handle_remove_column(self, event: object, view: str) -> str | None:
        tree = self.favorite_tree if view == "favorites" else self.recent_tree
        if tree.identify_column(event.x) != "#3":  # type: ignore[attr-defined]
            return None
        row = tree.identify_row(event.y)  # type: ignore[attr-defined]
        if not row:
            return None
        tree.selection_set(row)
        self.remove_selected_records()
        return "break"

    def _on_view_changed(self, _: object = None) -> None:
        if not hasattr(self, "status_var"):
            return
        view = self._active_view()
        self.settings.last_view = view
        self.settings.save()
        if view == "favorites":
            self._populate_favorites()
        elif view == "recent":
            self._populate_recent_files()
        elif self.project_root:
            self._populate_tree()
        else:
            self.status_var.set("请选择一个工程文件夹")

    def add_external_files(self) -> None:
        initial = str(self.project_root) if self.project_root else str(Path.home())
        chosen = filedialog.askopenfilenames(title="添加文件到快捷访问", initialdir=initial)
        if chosen:
            self._add_to_favorites([Path(value) for value in chosen])

    def add_external_folder(self) -> None:
        initial = str(self.project_root) if self.project_root else str(Path.home())
        chosen = filedialog.askdirectory(title="添加文件夹到快捷访问", initialdir=initial, mustexist=True)
        if chosen:
            self._add_to_favorites([Path(chosen)])

    def _add_to_favorites(self, paths: list[Path]) -> None:
        before = len(self.settings.favorite_paths)
        self.settings.favorite_paths = add_favorite_paths(self.settings.favorite_paths, paths)
        self.settings.save()
        self._populate_favorites()
        added = len(self.settings.favorite_paths) - before
        self.status_var.set(f"已添加 {added} 项到快捷访问" if added else "所选项目已在快捷访问中")

    def pin_selected(self) -> None:
        self._add_to_favorites(self._selected_paths())

    def remove_selected_records(self) -> None:
        view = self._active_view()
        paths = self._selected_paths()
        if not paths or view == "project":
            return
        if view == "favorites":
            self.settings.favorite_paths = remove_path_records(self.settings.favorite_paths, paths)
            self._populate_favorites()
        else:
            self.settings.recent_files = remove_path_records(self.settings.recent_files, paths)
            self._populate_recent_files()
        self.settings.save()
        self.status_var.set(f"已从列表移除 {len(paths)} 项；原文件未删除")

    def clear_recent_files(self) -> None:
        if not self.settings.recent_files:
            return
        if not messagebox.askyesno(APP_NAME, "清空全部最近打开记录？\n不会删除任何真实文件。"):
            return
        self.settings.recent_files = []
        self.settings.save()
        self._populate_recent_files()

    def toggle_recent_tracking(self) -> None:
        self.settings.track_recent_files = self.track_recent_var.get()
        self.settings.save()
        self._populate_recent_files()

    def _record_recent_file(self, path: Path) -> None:
        if not self.settings.track_recent_files or not path.is_file():
            return
        self.settings.recent_files = add_recent_file(self.settings.recent_files, path)
        self.settings.save()
        self._populate_recent_files()

    def set_selected_as_project(self) -> None:
        paths = self._selected_paths()
        if len(paths) == 1 and paths[0].is_dir():
            self.open_project(paths[0])

    def _bind_shortcuts(self) -> None:
        self.bind("<F5>", lambda _: self.refresh_project())
        self.bind("<Control-o>", lambda _: self.choose_project())
        self.bind("<Control-f>", lambda _: self.search_entry.focus_set())
        self.bind("<Control-l>", lambda _: self.search_var.set(""))

    def choose_project(self) -> None:
        initial = str(self.project_root) if self.project_root else str(Path.home())
        chosen = filedialog.askdirectory(title="选择工程文件夹", initialdir=initial, mustexist=True)
        if chosen:
            self.open_project(Path(chosen))

    def open_project(self, path: Path, switch_view: bool = True) -> None:
        path = path.resolve()
        if not path.is_dir():
            messagebox.showerror(APP_NAME, f"文件夹不存在：\n{path}")
            return
        self.project_root = path
        self.project_var.set(str(path))
        self.settings.last_project = str(path)
        recent = [str(path)] + [x for x in self.settings.recent_projects if Path(x) != path]
        self.settings.recent_projects = recent[:10]
        self.settings.save()
        self._update_recent_menu()
        if switch_view:
            self.view_notebook.select(self.project_tab)
        self.refresh_project()

    def refresh_project(self) -> None:
        if not self.project_root:
            return
        self.stop_event.set()
        self.stop_event = threading.Event()
        self.scan_generation += 1
        generation = self.scan_generation
        self.tree.delete(*self.tree.get_children())
        self.item_paths.clear()
        if self._active_view() == "project":
            self.status_var.set("正在扫描工程…")
        root = self.project_root
        ignored_directories = set(self.settings.ignored_directories)

        def worker() -> None:
            # 文件类型必须从整个工程动态生成，因此扫描阶段不提前过滤扩展名。
            records = scan_project(root, set(), ignored_directories, self.stop_event)
            self.result_queue.put((generation, records))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self) -> None:
        try:
            while True:
                generation, records = self.result_queue.get_nowait()
                if generation == self.scan_generation:
                    self.all_records = records
                    self._update_filter_summary()
                    self._populate_tree()
        except queue.Empty:
            pass
        self.after(100, self._poll_results)

    def _schedule_filter(self, _: object = None) -> None:
        if self.search_after_id:
            self.after_cancel(self.search_after_id)
        self.search_after_id = self.after(180, self._populate_tree)

    def _populate_tree(self) -> None:
        self.search_after_id = None
        self.tree.delete(*self.tree.get_children())
        self.item_paths.clear()
        if not self.project_root:
            return
        term = self.search_var.get().strip().casefold()
        if self.settings.filter_enabled:
            type_filtered = filter_records_by_type(
                self.all_records,
                self.settings.file_type_mode,
                set(self.settings.selected_file_types),
            )
        else:
            type_filtered = self.all_records
        if term:
            matched = [record for record in type_filtered if term in str(record.relative).casefold()]
            visible_paths: set[Path] = {record.relative for record in matched}
            for record in matched:
                visible_paths.update(record.relative.parents)
            relevant = [record for record in type_filtered if record.relative in visible_paths]
        else:
            # 无搜索时保留全部文件夹，表现与资源管理器一致；文件按类型规则过滤。
            relevant = type_filtered
        relevant.sort(key=lambda r: (len(r.relative.parts), tuple(x.casefold() for x in r.relative.parts), not r.is_dir))
        nodes: dict[Path, str] = {Path("."): ""}
        directory_count = 0
        file_count = 0
        for record in relevant:
            parent_relative = record.relative.parent
            parent_id = nodes.get(parent_relative, "")
            type_key = "<folder>" if record.is_dir else file_type_key(record.path)
            type_name = self.shell_icons.type_name(type_key)
            icon = self._icon_for(type_key)
            item = self.tree.insert(
                parent_id,
                END,
                text=record.path.name,
                values=(type_name, str(record.relative)),
                image=icon if icon else "",
                open=True,
            )
            self.item_paths[item] = record.path
            if record.is_dir:
                nodes[record.relative] = item
                directory_count += 1
            else:
                file_count += 1
        suffix = f"（搜索：{term}）" if term else ""
        self.view_notebook.tab(self.project_tab, text=f"  当前工程 ({file_count})  ")
        if self._active_view() == "project":
            self.status_var.set(f"{directory_count} 个文件夹，{file_count} 个文件 {suffix}")

    def _icon_for(self, type_key: str) -> PhotoImage | None:
        if type_key in self.tk_icons:
            return self.tk_icons[type_key]
        png = self.shell_icons.icon_png(type_key)
        if not png:
            return None
        try:
            image = PhotoImage(data=base64.b64encode(png).decode("ascii"))
        except Exception:
            return None
        self.tk_icons[type_key] = image
        return image

    def available_file_types(self) -> Counter[str]:
        return Counter(file_type_key(record.path) for record in self.all_records if not record.is_dir)

    def _update_filter_summary(self) -> None:
        count = len(self.settings.selected_file_types)
        if self.settings.filter_enabled:
            action = "只显示" if self.settings.file_type_mode == "show" else "忽略"
            self.filter_var.set(f"筛选中 · {action} {count} 种")
            self.filter_toggle_button.configure(text="关闭筛选", style="FilterOn.TButton")
            self.filter_badge.configure(style="ChipOn.TLabel")
        else:
            self.filter_var.set("筛选已关闭 · 显示全部文件")
            self.filter_toggle_button.configure(text="启用筛选", style="FilterOff.TButton")
            self.filter_badge.configure(style="ChipOff.TLabel")

    def toggle_filter(self) -> None:
        self.settings.filter_enabled = not self.settings.filter_enabled
        self.settings.save()
        self._update_filter_summary()
        self._populate_tree()

    def _set_all_open(self, is_open: bool) -> None:
        def visit(parent: str = "") -> None:
            for item in self.tree.get_children(parent):
                self.tree.item(item, open=is_open)
                visit(item)
        visit()

    def _selected_paths(self) -> list[Path]:
        tree = self._active_tree()
        item_paths = self._path_map_for_tree(tree)
        return [item_paths[item] for item in tree.selection() if item in item_paths]

    def _on_double_click(self, _: object) -> None:
        selected = self._selected_paths()
        if not selected:
            return
        if self._active_view() == "project" and selected[0].is_dir():
            return
        self.open_selected()

    def _show_context_menu(self, event: object) -> None:
        active_tree = self._active_tree()
        row = active_tree.identify_row(event.y)  # type: ignore[attr-defined]
        if row:
            if row not in active_tree.selection():
                active_tree.selection_set(row)
            selected = self._selected_paths()
            python_only = bool(selected) and all(p.is_file() and p.suffix.lower() == ".py" for p in selected)
            removable = self._active_view() in {"favorites", "recent"}
            can_set_project = len(selected) == 1 and selected[0].is_dir()
            self.context_menu.entryconfigure("固定到快捷访问", state="disabled" if self._active_view() == "favorites" else "normal")
            self.context_menu.entryconfigure("从当前列表移除", state="normal" if removable else "disabled")
            self.context_menu.entryconfigure("设为当前工程", state="normal" if can_set_project else "disabled")
            self.context_menu.entryconfigure("运行 Python 脚本", state="normal" if python_only else "disabled")
            self.context_menu.entryconfigure("运行 Python 脚本（窗口保留）", state="normal" if python_only else "disabled")
            self.context_menu.tk_popup(event.x_root, event.y_root)  # type: ignore[attr-defined]

    def open_selected(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        errors: list[str] = []
        for path in paths:
            try:
                self._open_path(path)
                self._record_recent_file(path)
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
        if errors:
            messagebox.showerror(APP_NAME, "部分文件无法打开：\n" + "\n".join(errors[:8]))

    @staticmethod
    def _open_path(path: Path) -> None:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def reveal_selected(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        path = paths[0]
        if os.name == "nt":
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["explorer", str(path)])
        else:
            self._open_path(path.parent if path.is_file() else path)

    def _working_directory(self) -> Path | None:
        paths = self._selected_paths()
        if paths:
            return paths[0] if paths[0].is_dir() else paths[0].parent
        return self.project_root

    def open_cmd_here(self) -> None:
        directory = self._working_directory()
        if directory:
            subprocess.Popen(["cmd.exe", "/K"], cwd=str(directory), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def open_powershell_here(self) -> None:
        directory = self._working_directory()
        if directory:
            subprocess.Popen(["powershell.exe", "-NoExit"], cwd=str(directory), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def run_python_selected(self, keep_open: bool = False) -> None:
        paths = [p for p in self._selected_paths() if p.is_file() and p.suffix.lower() == ".py"]
        python_executable = Path(sys.executable)
        if python_executable.name.casefold() == "pythonw.exe":
            console_python = python_executable.with_name("python.exe")
            if console_python.exists():
                python_executable = console_python
        for path in paths:
            command = ["cmd.exe", "/K" if keep_open else "/C", str(python_executable), str(path)]
            subprocess.Popen(command, cwd=str(path.parent), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            self._record_recent_file(path)

    def copy_paths(self) -> None:
        paths = self._selected_paths()
        if paths:
            self.clipboard_clear()
            self.clipboard_append("\n".join(str(path) for path in paths))
            self.status_var.set(f"已复制 {len(paths)} 个路径")

    def _update_selection_status(self, _: object = None) -> None:
        count = len(self.tree.selection())
        if count:
            self.status_var.set(f"已选择 {count} 项；按 Enter 或双击打开")

    def open_file_type_dialog(self) -> None:
        FileTypeDialog(self)

    def _update_recent_menu(self) -> None:
        self.recent_menu.delete(0, END)
        valid = [p for p in self.settings.recent_projects if Path(p).is_dir()]
        if not valid:
            self.recent_menu.add_command(label="暂无", state="disabled")
            return
        for path in valid:
            self.recent_menu.add_command(label=path, command=lambda p=path: self.open_project(Path(p)))

    def _on_close(self) -> None:
        self.stop_event.set()
        self.settings.window_geometry = self.geometry()
        self.settings.save()
        self.destroy()


def main() -> None:
    app = SeekOpenApp()
    app.mainloop()


if __name__ == "__main__":
    main()
