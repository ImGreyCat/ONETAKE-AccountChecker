import os
import re
import sys
import ctypes
import datetime
import urllib.request
import urllib.error
import http.cookiejar
import gzip
import zlib
import subprocess
import time
import webbrowser

if sys.platform == "win32":
    import winreg

STEAM64_BASE = 76561197960265728


# ============================================================
# РЕСУРСЫ
# ============================================================

def resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


def set_console_icon(icon_path):
    if sys.platform != "win32":
        return
    if not os.path.exists(icon_path):
        return
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        hicon_big = user32.LoadImageW(
            None, icon_path, IMAGE_ICON, 0, 0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
        hicon_small = user32.LoadImageW(
            None, icon_path, IMAGE_ICON, 16, 16,
            LR_LOADFROMFILE
        )
        WM_SETICON = 0x0080
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, 1, hicon_big)
        if hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, 0, hicon_small)
    except Exception:
        pass


def set_console_title(title):
    try:
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        else:
            sys.stdout.write(f"\033]0;{title}\007")
            sys.stdout.flush()
    except Exception:
        pass


# ============================================================
# ПОИСК STEAM
# ============================================================

def _windows_steam_path():
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Valve\Steam"),
    ]
    for hive, subkey in candidates:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                for value_name in ("InstallPath", "SteamPath"):
                    try:
                        path, _ = winreg.QueryValueEx(key, value_name)
                        if path and os.path.isdir(path):
                            return path
                    except Exception:
                        continue
        except Exception:
            continue
    return None


def _linux_steam_paths():
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".steam", "root"),
        os.path.join(home, ".local", "share", "Steam"),
        os.path.join(home, ".var", "app", "com.valvesoftware.Steam",
                     "data", "Steam"),
        os.path.join(home, "snap", "steam", "common", ".local",
                     "share", "Steam"),
        os.path.join(home, ".steam", "debian-instance"),
    ]


def _macos_steam_path():
    return os.path.join(os.path.expanduser("~"),
                        "Library", "Application Support", "Steam")


def get_steam_path():
    if sys.platform == "win32":
        path = _windows_steam_path()
        if path:
            return path
        for candidate in (
            r"C:\Program Files (x86)\Steam",
            r"C:\Program Files\Steam",
        ):
            if os.path.isdir(candidate):
                return candidate
        return r"C:\Program Files (x86)\Steam"

    if sys.platform == "darwin":
        return _macos_steam_path()

    for path in _linux_steam_paths():
        if os.path.isdir(path):
            return path

    return _linux_steam_paths()[0]


def steam3_to_steam64(steam3_id):
    try:
        return str(int(steam3_id) + STEAM64_BASE)
    except (ValueError, TypeError):
        return "—"


def parse_loginusers(steam_path):
    loginusers_path = os.path.join(steam_path, "config", "loginusers.vdf")
    accounts = {}
    if not os.path.exists(loginusers_path):
        return accounts
    try:
        with open(loginusers_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        pattern = re.compile(
            r'"(7656119\d{10})"\s*\{[^}]*?"AccountName"\s*"([^"]+)"',
            re.DOTALL
        )
        for steam_id64, account_name in pattern.findall(content):
            steam3_id = str(int(steam_id64) - STEAM64_BASE)
            accounts[steam3_id] = account_name
    except Exception as e:
        print(f"  [!] Ошибка чтения loginusers.vdf: {e}")
    return accounts


def get_persona_name(steam_path, steam3_id):
    localconfig = os.path.join(steam_path, "userdata", steam3_id,
                               "config", "localconfig.vdf")
    if not os.path.exists(localconfig):
        return "—"
    try:
        with open(localconfig, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        match = re.search(r'"PersonaName"\s*"([^"]+)"', content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "—"


def get_last_login(steam_path, steam3_id):
    loginusers_path = os.path.join(steam_path, "config", "loginusers.vdf")
    if not os.path.exists(loginusers_path):
        return "—"
    try:
        with open(loginusers_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        steam_id64 = str(int(steam3_id) + STEAM64_BASE)
        block_pattern = re.compile(r'"' + steam_id64 + r'"\s*\{(.*?)\}',
                                   re.DOTALL)
        block = block_pattern.search(content)
        if block:
            ts_match = re.search(r'"Timestamp"\s*"(\d+)"', block.group(1))
            if ts_match:
                return datetime.datetime.fromtimestamp(
                    int(ts_match.group(1))
                ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        pass
    return "—"


# ============================================================
# VAC
# ============================================================

def check_vac_ban_xml(steamid64):
    url = f"https://steamcommunity.com/profiles/{steamid64}/?xml=1"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read().decode("utf-8", errors="ignore")
        match = re.search(r"<vacBanned>(\d+)</vacBanned>", xml_data)
        if match:
            return "Да" if match.group(1) == "1" else "Нет"
        return "?"
    except Exception:
        return "?"


# ============================================================
# ONETAKE
# ============================================================

_ONETAKE_OPENER = None


def _get_onetake_opener():
    global _ONETAKE_OPENER
    if _ONETAKE_OPENER is None:
        jar = http.cookiejar.CookieJar()
        _ONETAKE_OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
    return _ONETAKE_OPENER


def _fetch_onetake(url, timeout=15):
    opener = _get_onetake_opener()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) "
            "Gecko/20100101 Firefox/155.0"
        ),
        "Accept": ("text/html,application/xhtml+xml,"
                   "application/xml;q=0.9,*/*;q=0.8"),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "DNT": "1",
    }
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as response:
        raw = response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return raw.decode("utf-8", errors="ignore")


def _parse_onetake_section(html, section_title):
    idx = html.find(section_title)
    if idx == -1:
        return [], 0

    total = 0
    m = re.search(
        re.escape(section_title) + r"\s*\((\d+)\)",
        html[idx:idx + 300]
    )
    if m:
        total = int(m.group(1))

    content_idx = html.find("bans_comms_content", idx)
    if content_idx == -1:
        return [], total

    next_section = html.find("title_head", content_idx + 20)
    if next_section == -1:
        fragment = html[content_idx:content_idx + 50000]
    else:
        fragment = html[content_idx:next_section]

    entries = []
    for li_match in re.finditer(r"<li>(.*?)</li>", fragment, re.DOTALL):
        li = li_match.group(1)
        if "Дата" in li and "Причина" in li:
            continue

        hide_spans = []
        for sm in re.finditer(
            r'<span[^>]*class="[^"]*\bhide_this\b[^"]*"[^>]*>(.*?)</span>',
            li, re.DOTALL
        ):
            txt = re.sub(r"<[^>]+>", "", sm.group(1)).strip()
            hide_spans.append(txt)

        reason = ""
        rm = re.search(r"<span>([^<]+)</span>", li)
        if rm:
            reason = rm.group(1).strip()

        status_class = ""
        status_text = ""
        sm = re.search(
            r'<span class="exp_badge\s*([^"]*)"[^>]*>\s*([^<]+?)\s*</span>',
            li, re.DOTALL
        )
        if sm:
            status_class = sm.group(1).strip()
            status_text = sm.group(2).strip()

        entries.append({
            "date": hide_spans[0] if len(hide_spans) > 0 else "",
            "reason": reason,
            "admin": hide_spans[1] if len(hide_spans) > 1 else "",
            "extra": hide_spans[2] if len(hide_spans) > 2 else "",
            "status": status_text,
            "status_class": status_class,
            "active": "unbanned" not in status_class,
        })

    return entries, total


def check_onetake_ban(steamid64):
    result = {
        "status": "?",
        "bans": [],
        "mutes": [],
        "bans_total": 0,
        "mutes_total": 0,
        "error": None,
    }

    url = f"https://onetake-cs2.ru/profiles/{steamid64}/block/0/"

    try:
        html = _fetch_onetake(url)
    except urllib.error.HTTPError as e:
        result["error"] = f"HTTP {e.code}"
        return result
    except Exception as e:
        result["error"] = str(e)
        return result

    if "Профиль не найден" in html:
        result["status"] = "Не найден"
        return result

    if "Последние баны" not in html and "Последние муты" not in html:
        if "ddos-guard" in html.lower():
            result["error"] = "DDoS-Guard блокировка"
        else:
            result["error"] = "Не удалось найти данные о банах"
        return result

    bans, bans_total = _parse_onetake_section(html, "Последние баны")
    mutes, mutes_total = _parse_onetake_section(html, "Последние муты")

    result["bans"] = bans
    result["mutes"] = mutes
    result["bans_total"] = bans_total
    result["mutes_total"] = mutes_total

    active = ([b for b in bans if b["active"]]
              + [m for m in mutes if m["active"]])
    result["status"] = "Да" if active else "Нет"
    return result


def open_onetake_profile(steamid64):
    url = f"https://onetake-cs2.ru/profiles/{steamid64}/?search=1"
    try:
        webbrowser.open(url)
        return True, url
    except Exception as e:
        return False, str(e)


# ============================================================
# ЦВЕТА
# ============================================================

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"


def enable_ansi():
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


# ============================================================
# РАБОЧИЙ СТОЛ
# ============================================================

def _windows_desktop_path():
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer"
            r"\User Shell Folders"
        ) as key:
            path, _ = winreg.QueryValueEx(key, "Desktop")
            path = os.path.expandvars(path)
            if os.path.isdir(path):
                return path
    except Exception:
        pass

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(desktop):
        return desktop

    onedrive = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop")
    if os.path.isdir(onedrive):
        return onedrive

    try:
        import ctypes.wintypes
        buf = ctypes.create_unicode_buffer(260)
        CSIDL_DESKTOPDIRECTORY = 0x0010
        ctypes.windll.shell32.SHGetFolderPathW(
            None, CSIDL_DESKTOPDIRECTORY, None, 0, buf
        )
        if buf.value and os.path.isdir(buf.value):
            return buf.value
    except Exception:
        pass

    return os.path.expanduser("~")


def _linux_desktop_path():
    try:
        result = subprocess.run(
            ["xdg-user-dir", "DESKTOP"],
            capture_output=True, text=True, timeout=3
        )
        path = result.stdout.strip()
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass

    try:
        config_file = os.path.join(os.path.expanduser("~"), ".config",
                                   "user-dirs.dirs")
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                for line in f:
                    m = re.match(r'\s*XDG_DESKTOP_DIR\s*=\s*"([^"]+)"', line)
                    if m:
                        path = os.path.expandvars(m.group(1))
                        path = os.path.expanduser(path)
                        if os.path.isdir(path):
                            return path
    except Exception:
        pass

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(desktop):
        return desktop

    return os.path.expanduser("~")


def get_desktop_path():
    if sys.platform == "win32":
        return _windows_desktop_path()
    if sys.platform == "darwin":
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        return desktop if os.path.isdir(desktop) else os.path.expanduser("~")
    return _linux_desktop_path()


# ============================================================
# ЭКСПОРТ
# ============================================================

def export_to_txt(accounts, file_path):
    try:
        parent = os.path.dirname(os.path.abspath(file_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write("=" * 110 + "\n")
            f.write("ONETAKE ACCOUNT MANAGER\n")
            f.write("СПИСОК АККАУНТОВ STEAM\n")
            f.write("=" * 110 + "\n")
            f.write(f"Дата экспорта  : "
                    f"{datetime.datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n")
            f.write(f"Всего аккаунтов: {len(accounts)}\n")
            f.write("=" * 110 + "\n\n")

            f.write(f"{'№':<4}{'SteamID64':<20}{'Логин':<22}"
                    f"{'Ник в Steam':<25}{'Последний вход':<18}"
                    f"{'VAC':<6}{'ONETAKE'}\n")
            f.write("-" * 120 + "\n")

            for i, acc in enumerate(accounts, start=1):
                f.write(
                    f"{i:<4}{acc['steamid64']:<20}{acc['login']:<22}"
                    f"{acc['persona']:<25}{acc['last_login']:<18}"
                    f"{acc['vac']:<6}{acc.get('onetake', '?')}\n"
                )

            f.write("\n" + "=" * 110 + "\n")
            f.write("ТОЛЬКО STEAMID64:\n")
            f.write("=" * 110 + "\n")
            for acc in accounts:
                f.write(f"{acc['steamid64']}\n")

            f.write("\n" + "=" * 110 + "\n")
            f.write("ССЫЛКИ НА ПРОФИЛИ ONETAKE:\n")
            f.write("=" * 110 + "\n")
            for acc in accounts:
                f.write(f"{acc['login']:<25} "
                        f"https://onetake-cs2.ru/profiles/"
                        f"{acc['steamid64']}/?search=1\n")

            onetake_banned = [a for a in accounts
                              if a.get("onetake") == "Да"]
            if onetake_banned:
                f.write("\n" + "=" * 110 + "\n")
                f.write("ДЕТАЛИ БАНОВ ONETAKE:\n")
                f.write("=" * 110 + "\n")
                for acc in onetake_banned:
                    f.write(f"\n[{acc['steamid64']}] "
                            f"{acc['persona']} / {acc['login']}\n")
                    info = acc.get("onetake_info")
                    if not info:
                        continue
                    for b in info.get("bans", []):
                        if b["active"]:
                            f.write(f"  БАН  | {b['date']} | "
                                    f"{b['reason']} | админ: {b['admin']} | "
                                    f"{b['status']}\n")
                    for mu in info.get("mutes", []):
                        if mu["active"]:
                            f.write(f"  МУТ  | {mu['date']} | "
                                    f"{mu['reason']} | админ: {mu['admin']} | "
                                    f"{mu['status']}\n")

        return True, file_path
    except Exception as e:
        return False, str(e)


# ============================================================
# ВЫВОД
# ============================================================

def print_header():
    print()
    print(f"{C.CYAN}{C.BOLD}{'=' * 110}{C.RESET}")
    print(f"{C.CYAN}{C.BOLD}  ONETAKE Account manager  —  список аккаунтов Steam, "
          f"VAC-баны и баны ONETAKE{C.RESET}")
    print(f"{C.CYAN}{C.BOLD}{'=' * 110}{C.RESET}")
    print()


def print_accounts(accounts):
    header = (
        f"{C.BOLD}"
        f"{'№':<4}"
        f"{'SteamID64':<20}"
        f"{'Логин':<22}"
        f"{'Ник в Steam':<25}"
        f"{'Последний вход':<18}"
        f"{'VAC':<7}"
        f"{'ONETAKE':<10}"
        f"{C.RESET}"
    )
    print(header)
    print(f"{C.GRAY}{'-' * 110}{C.RESET}")

    banned_vac = 0
    banned_onetake = 0
    for i, acc in enumerate(accounts, start=1):
        steamid64 = acc["steamid64"]
        login = acc["login"]
        persona = acc["persona"]
        last_login = acc["last_login"]
        vac = acc["vac"]
        onetake = acc.get("onetake", "?")

        if vac == "Да":
            vac_display = f"{C.RED}{C.BOLD}Да{C.RESET}"
            banned_vac += 1
        elif vac == "Нет":
            vac_display = f"{C.GREEN}Нет{C.RESET}"
        else:
            vac_display = f"{C.YELLOW}?{C.RESET}"

        if onetake == "Да":
            onetake_display = f"{C.RED}{C.BOLD}Да{C.RESET}"
            banned_onetake += 1
        elif onetake == "Нет":
            onetake_display = f"{C.GREEN}Нет{C.RESET}"
        elif onetake == "Не найден":
            onetake_display = f"{C.GRAY}нет проф.{C.RESET}"
        else:
            onetake_display = f"{C.YELLOW}?{C.RESET}"

        if len(persona) > 24:
            persona = persona[:21] + "..."
        if len(login) > 21:
            login = login[:18] + "..."

        print(
            f"{i:<4}"
            f"{steamid64:<20}"
            f"{login:<22}"
            f"{persona:<25}"
            f"{last_login:<18}"
            f"{vac_display:<16}"
            f"{onetake_display}"
        )

    return banned_vac, banned_onetake


def print_footer(total, banned_vac, banned_onetake):
    print(f"{C.GRAY}{'-' * 110}{C.RESET}")
    print()
    print(f"  Всего аккаунтов:     {C.BOLD}{total}{C.RESET}")
    print(f"  С VAC-баном:         {C.RED}{C.BOLD}{banned_vac}{C.RESET}")
    print(f"  С баном на ONETAKE:  {C.RED}{C.BOLD}{banned_onetake}{C.RESET}")
    print()


# ============================================================
# ГЛАВНОЕ МЕНЮ (2 пункта)
# ============================================================

def main_menu(accounts):
    """
    1 — выбрать аккаунт для открытия сайта ONETAKE
    2 — экспортировать на рабочий стол
    0 — выход
    """
    while True:
        print(f"  {C.BOLD}Меню:{C.RESET}")
        print(f"    {C.CYAN}1{C.RESET} — Выбрать аккаунт для открытия "
              f"сайта ONETAKE")
        print(f"    {C.CYAN}2{C.RESET} — Экспортировать на рабочий стол")
        print(f"    {C.CYAN}0{C.RESET} — Выход")
        print()

        try:
            choice = input("  Ваш выбор [0]: ").strip() or "0"
        except EOFError:
            return

        if choice == "0":
            print()
            return

        # ---- Пункт 1: выбор аккаунта ----
        if choice == "1":
            try:
                num = input("  Введите номер аккаунта "
                            "(или 0 для отмены): ").strip()
            except EOFError:
                continue

            if not num.isdigit():
                print(f"  {C.YELLOW}Нужно ввести число.{C.RESET}\n")
                continue

            idx = int(num)
            if idx == 0:
                print()
                continue

            if idx < 1 or idx > len(accounts):
                print(f"  {C.YELLOW}Аккаунта с таким номером нет "
                      f"(всего: {len(accounts)}).{C.RESET}\n")
                continue

            acc = accounts[idx - 1]
            steamid64 = acc["steamid64"]
            login = acc["login"]
            persona = acc["persona"]

            ok, url = open_onetake_profile(steamid64)
            if ok:
                print(f"\n  {C.GREEN}[✓] Открываю {C.BOLD}{login}{C.RESET}"
                      f"{C.GREEN} ({persona}) в браузере:{C.RESET}")
                print(f"      {url}\n")
            else:
                print(f"\n  {C.RED}[!] Ошибка: {url}{C.RESET}\n")
            continue

        # ---- Пункт 2: экспорт на рабочий стол ----
        if choice == "2":
            desktop = get_desktop_path()
            filename = (f"steam_accounts_"
                        f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
                        f".txt")
            path = os.path.join(desktop, filename)

            ok, msg = export_to_txt(accounts, path)
            if ok:
                print(f"\n  {C.GREEN}[✓] Файл сохранён на рабочий стол:"
                      f"{C.RESET}")
                print(f"      {msg}\n")
            else:
                print(f"\n  {C.RED}[!] Ошибка: {msg}{C.RESET}\n")
            continue

        print(f"  {C.YELLOW}Неверный выбор. Введите 0, 1 или 2.{C.RESET}\n")


# ============================================================
# ГЛАВНАЯ
# ============================================================

def main():
    enable_ansi()
    set_console_title("ONETAKE Account Manager")
    set_console_icon(resource_path("cmd.ico"))

    print_header()

    steam_path = get_steam_path()
    print(f"  {C.DIM}ОС:{C.RESET} {sys.platform}")
    print(f"  {C.DIM}Путь Steam:{C.RESET} {steam_path}")
    print()

    userdata_path = os.path.join(steam_path, "userdata")
    if not os.path.exists(userdata_path):
        print(f"{C.RED}  [!] Папка userdata не найдена: "
              f"{userdata_path}{C.RESET}")
        print()
        try:
            input("  Нажмите Enter для выхода...")
        except EOFError:
            pass
        return

    account_map = parse_loginusers(steam_path)
    raw_accounts = []
    for folder_name in sorted(os.listdir(userdata_path)):
        folder_path = os.path.join(userdata_path, folder_name)
        if not (os.path.isdir(folder_path) and folder_name.isdigit()):
            continue
        raw_accounts.append(folder_name)

    if not raw_accounts:
        print(f"{C.YELLOW}  Аккаунты не найдены.{C.RESET}")
        print()
        try:
            input("  Нажмите Enter для выхода...")
        except EOFError:
            pass
        return

    print(f"  {C.DIM}Найдено папок с аккаунтами: {len(raw_accounts)}. "
          f"Проверка VAC-банов и банов на ONETAKE...{C.RESET}")
    print()

    accounts = []
    for i, steam3_id in enumerate(raw_accounts, start=1):
        steamid64 = steam3_to_steam64(steam3_id)
        login = account_map.get(steam3_id, "Неизвестно")
        persona = get_persona_name(steam_path, steam3_id)
        last_login = get_last_login(steam_path, steam3_id)

        print(f"  [{i}/{len(raw_accounts)}] VAC {steamid64}...       ",
              end="\r")
        vac = check_vac_ban_xml(steamid64)

        print(f"  [{i}/{len(raw_accounts)}] ONETAKE {steamid64}...    ",
              end="\r")
        onetake_info = check_onetake_ban(steamid64)
        onetake = onetake_info["status"]

        time.sleep(0.4)

        accounts.append({
            "steamid64": steamid64,
            "login": login,
            "persona": persona,
            "last_login": last_login,
            "vac": vac,
            "onetake": onetake,
            "onetake_info": onetake_info,
        })

    print(" " * 70, end="\r")
    print()

    banned_vac, banned_onetake = print_accounts(accounts)
    print_footer(len(accounts), banned_vac, banned_onetake)

    onetake_banned = [a for a in accounts if a.get("onetake") == "Да"]
    if onetake_banned:
        print(f"  {C.BOLD}{C.RED}Детали активных банов ONETAKE:"
              f"{C.RESET}\n")
        for acc in onetake_banned:
            print(f"  {C.BOLD}[{acc['steamid64']}] "
                  f"{acc['persona']} / {acc['login']}{C.RESET}")
            info = acc.get("onetake_info", {})
            for b in info.get("bans", []):
                if b["active"]:
                    print(f"    {C.RED}БАН{C.RESET}  | {b['date']} | "
                          f"{b['reason']} | админ: {b['admin']} | "
                          f"{C.RED}{b['status']}{C.RESET}")
            for mu in info.get("mutes", []):
                if mu["active"]:
                    print(f"    {C.YELLOW}МУТ{C.RESET}  | {mu['date']} | "
                          f"{mu['reason']} | админ: {mu['admin']} | "
                          f"{mu['status']}")
            print()

    # ЕДИНСТВЕННОЕ МЕНЮ
    main_menu(accounts)

    try:
        input("  Нажмите Enter для выхода...")
    except EOFError:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Прервано пользователем.")
        sys.exit(0)
    except Exception as e:
        print(f"\n  {C.RED}[!] Неожиданная ошибка: {e}{C.RESET}")
        try:
            input("  Нажмите Enter для выхода...")
        except EOFError:
            pass