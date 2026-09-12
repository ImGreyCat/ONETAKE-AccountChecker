# ============================================================
#  ONETAKE Account Manager
#  Собирает аккаунты Steam из userdata, проверяет VAC-баны и
#  баны на проекте ONETAKE.
# ============================================================

import os
import re
import sys
import json
import ctypes
import datetime
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
import gzip
import zlib
import subprocess
import time
import webbrowser

# winreg есть только в Windows
if sys.platform == "win32":
    import winreg

# Смещение: SteamID64 = SteamID3 + это число
STEAM64_BASE = 76561197960265728


# ============================================================
#  ЦВЕТА КОНСОЛИ
# ============================================================

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"


def enable_ansi():
    """Включает поддержку ANSI-цветов в консоли Windows."""
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


# ============================================================
#  ПОИСК STEAM
# ============================================================

def get_steam_path():
    """
    Ищет папку Steam.
    Windows — через реестр, Linux/macOS — по типовым путям.
    """
    # --- Windows ---
    if sys.platform == "win32":
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        ]
        for hive, subkey in keys:
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    path, _ = winreg.QueryValueEx(k, "InstallPath")
                    if os.path.isdir(path):
                        return path
            except Exception:
                continue
        return r"C:\Program Files (x86)\Steam"

    # --- Linux / macOS ---
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".local", "share", "Steam"),
        os.path.join(home, ".var", "app", "com.valvesoftware.Steam",
                     "data", "Steam"),
        os.path.join(home, "Library", "Application Support", "Steam"),
    ]
    for p in candidates:
        if os.path.isdir(p):
            return p

    return os.path.join(home, ".steam", "steam")


def steam3_to_steam64(steam3_id):
    """SteamID3 (число из имени папки) → SteamID64 (строка)."""
    try:
        return str(int(steam3_id) + STEAM64_BASE)
    except (ValueError, TypeError):
        return "—"


# ============================================================
#  ЧТЕНИЕ ДАННЫХ ИЗ ФАЙЛОВ STEAM
# ============================================================

def parse_loginusers(steam_path):
    """Читает config/loginusers.vdf → {steam3_id: логин}."""
    path = os.path.join(steam_path, "config", "loginusers.vdf")
    accounts = {}
    if not os.path.exists(path):
        return accounts

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # "76561199..." { ... "AccountName" "login" ... }
        pattern = re.compile(
            r'"(7656119\d{10})"\s*\{[^}]*?"AccountName"\s*"([^"]+)"',
            re.DOTALL
        )
        for steam_id64, login in pattern.findall(content):
            steam3 = str(int(steam_id64) - STEAM64_BASE)
            accounts[steam3] = login
    except Exception:
        pass

    return accounts


def get_persona_name(steam_path, steam3_id):
    """Читает ник из userdata/<id>/config/localconfig.vdf."""
    path = os.path.join(steam_path, "userdata", steam3_id,
                        "config", "localconfig.vdf")
    if not os.path.exists(path):
        return "—"

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            m = re.search(r'"PersonaName"\s*"([^"]+)"', f.read())
            return m.group(1) if m else "—"
    except Exception:
        return "—"


# ============================================================
#  ПРОВЕРКА VAC-БАНОВ
# ============================================================

def check_vac(steamid64):
    """Ищет <vacBanned> в Steam Community XML. Возвращает Да/Нет/?."""
    url = f"https://steamcommunity.com/profiles/{steamid64}/?xml=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read().decode("utf-8", errors="ignore")

        m = re.search(r"<vacBanned>(\d+)</vacBanned>", data)
        if not m:
            return "?"
        return "Да" if m.group(1) == "1" else "Нет"
    except Exception:
        return "?"


# ============================================================
#  ПРОВЕРКА БАНОВ/МУТОВ НА ONETAKE-CS2.RU
# ============================================================
#
#  Использует POST-эндпоинт /punishment/?num=1.
#  Тело запроса: search_ban=<id>&search_mute=&search_ctban=&num=1
#  Ответ — JSON:
#    {
#      "results": [
#         {"sid": "<SteamID64 цели>", "check_getavatar": 0,
#          "search_html": "<li>...</li>"},
#         ...
#      ],
#      "total": N
#    }
#
#  ВАЖНО: если искомый steamid — админ, то в результатах будут
#  и баны НА него, и баны ОТ него. Нам нужны только записи,
#  где sid == искомый steamid (т.е. игрок = цель наказания).
#
#  В search_html spans идут в порядке:
#    [svg-иконка] [аватар] [ник цели] [причина] [срок+класс] [ник админа]
#  Срок считается активным, если его класс содержит
#  current_punish (временный) или permanent_punish (навсегда).

_onetake_opener = None


def _get_opener():
    """
    Возвращает общий opener с cookie jar.
    Один раз делает GET на главную, чтобы DDoS-Guard выдал cookies.
    """
    global _onetake_opener
    if _onetake_opener is not None:
        return _onetake_opener

    jar = http.cookiejar.CookieJar()
    _onetake_opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar)
    )

    # Разогрев: получаем cookies от DDoS-Guard
    try:
        req = urllib.request.Request(
            "https://onetake-cs2.ru/",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        _onetake_opener.open(req, timeout=10).read()
    except Exception:
        pass

    return _onetake_opener


def _http_post(url, data, timeout=15):
    """
    POST-запрос с form-urlencoded и cookies.
    Распаковывает gzip/deflate. Возвращает строку ответа.
    """
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) "
                       "Gecko/20100101 Firefox/155.0"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://onetake-cs2.ru/punishment/?num=1",
    })

    with _get_opener().open(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return raw.decode("utf-8", errors="ignore")


def _extract_punish_info(html):
    """
    Из HTML одного <li> вытаскивает данные наказания.
    Возвращает {nick, reason, duration, admin, active}.
    """
    info = {"nick": "", "reason": "", "duration": "",
            "admin": "", "active": False}

    # Активен, если у срока класс current_punish или permanent_punish
    if "current_punish" in html or "permanent_punish" in html:
        info["active"] = True

    # Все "простые" <span>текст</span> — без вложенных тегов.
    # Именно так отсеиваются span'ы с иконкой и аватаркой.
    plain = re.findall(r'<span[^>]*>([^<]+)</span>', html)
    plain = [s.strip() for s in plain if s.strip()]

    if len(plain) >= 1:
        info["nick"] = plain[0]        # ник цели
    if len(plain) >= 2:
        info["reason"] = plain[1]      # причина
    if len(plain) >= 3:
        info["duration"] = plain[2]    # срок (напр. "Навсегда", "59 мин.")
    if len(plain) >= 4:
        info["admin"] = plain[3]       # ник админа

    return info


def _query_punishments(steamid64, kind="ban", page=1):
    """
    Отправляет POST на /punishment/ и возвращает распарсенный JSON.
    kind: 'ban' | 'mute' | 'ctban'
    Возвращает dict или None при ошибке.
    """
    url = f"https://onetake-cs2.ru/punishment/?num={page}"
    data = {
        "search_ban":   steamid64 if kind == "ban"   else "",
        "search_mute":  steamid64 if kind == "mute"  else "",
        "search_ctban": steamid64 if kind == "ctban" else "",
        "num": str(page),
    }
    try:
        text = _http_post(url, data)
        return json.loads(text)
    except Exception:
        return None


def check_onetake(steamid64):
    """
    Проверяет баны и муты на onetake-cs2.ru через /punishment/.
    Возвращает {'status', 'bans', 'mutes'}.
    status: 'Да' | 'Нет' | '?' (не удалось получить ответ)
    """
    result = {"status": "?", "bans": [], "mutes": []}
    got_response = False

    # Отдельно запрашиваем баны и муты — эндпоинт ищет по разным полям
    for kind, key in (("ban", "bans"), ("mute", "mutes")):
        data = _query_punishments(steamid64, kind, page=1)
        if data is None:
            continue
        got_response = True

        for item in data.get("results", []):
            # Нам нужны только те записи, где цель = наш игрок.
            # Записи, где наш игрок — админ (выдал бан другому),
            # имеют sid другого человека.
            if item.get("sid") != steamid64:
                continue

            info = _extract_punish_info(item.get("search_html", ""))
            result[key].append(info)

    if not got_response:
        return result

    active = [e for e in result["bans"] + result["mutes"] if e["active"]]
    result["status"] = "Да" if active else "Нет"
    return result


def open_profile(steamid64):
    """Открывает профиль игрока на ONETAKE в браузере."""
    url = f"https://onetake-cs2.ru/profiles/{steamid64}/?search=1"
    try:
        webbrowser.open(url)
        return url
    except Exception:
        return None


# ============================================================
#  РАБОЧИЙ СТОЛ
# ============================================================

def get_desktop():
    """Возвращает путь к рабочему столу или домашней папке."""
    home = os.path.expanduser("~")
    for p in [
        os.path.join(home, "Desktop"),
        os.path.join(home, "Рабочий стол"),
        os.path.join(home, "OneDrive", "Desktop"),
        home,
    ]:
        if os.path.isdir(p):
            return p
    return home


# ============================================================
#  ЭКСПОРТ В TXT
# ============================================================

def export_txt(accounts, path):
    """Сохраняет список аккаунтов в текстовый файл."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("ONETAKE ACCOUNT MANAGER\n")
            f.write(f"Дата: {datetime.datetime.now():%d.%m.%Y %H:%M:%S}\n")
            f.write(f"Всего аккаунтов: {len(accounts)}\n\n")

            f.write(f"{'№':<4}{'SteamID64':<20}{'Логин':<22}"
                    f"{'Ник':<25}{'VAC':<6}{'ONETAKE'}\n")
            f.write("-" * 100 + "\n")
            for i, a in enumerate(accounts, 1):
                f.write(f"{i:<4}{a['steamid64']:<20}{a['login']:<22}"
                        f"{a['persona']:<25}{a['vac']:<6}"
                        f"{a['onetake']}\n")

            f.write("\n\nSTEAMID64:\n")
            for a in accounts:
                f.write(a["steamid64"] + "\n")

            f.write("\n\nССЫЛКИ НА ПРОФИЛИ ONETAKE:\n")
            for a in accounts:
                f.write(f"https://onetake-cs2.ru/profiles/"
                        f"{a['steamid64']}/?search=1\n")
        return True
    except Exception as e:
        print(f"  {C.RED}[!] Ошибка экспорта: {e}{C.RESET}")
        return False


# ============================================================
#  ВЫВОД В КОНСОЛЬ
# ============================================================

def print_table(accounts):
    """Печатает таблицу аккаунтов и краткую статистику."""
    print(f"{C.BOLD}{'№':<4}{'SteamID64':<20}{'Логин':<22}"
          f"{'Ник':<25}{'VAC':<7}{'ONETAKE'}{C.RESET}")
    print(f"{C.GRAY}{'-' * 100}{C.RESET}")

    vac_count = 0
    onetake_count = 0

    for i, a in enumerate(accounts, 1):
        if a["vac"] == "Да":
            vac_disp = f"{C.RED}Да{C.RESET}"
            vac_count += 1
        elif a["vac"] == "Нет":
            vac_disp = f"{C.GREEN}Нет{C.RESET}"
        else:
            vac_disp = f"{C.YELLOW}?{C.RESET}"

        if a["onetake"] == "Да":
            ot_disp = f"{C.RED}Да{C.RESET}"
            onetake_count += 1
        elif a["onetake"] == "Нет":
            ot_disp = f"{C.GREEN}Нет{C.RESET}"
        else:
            ot_disp = f"{C.YELLOW}?{C.RESET}"

        login = a["login"][:21]
        persona = a["persona"][:24]

        print(f"{i:<4}{a['steamid64']:<20}{login:<22}"
              f"{persona:<25}{vac_disp:<16}{ot_disp}")

    print()
    print(f"  Всего: {C.BOLD}{len(accounts)}{C.RESET}  |  "
          f"VAC: {C.RED}{vac_count}{C.RESET}  |  "
          f"ONETAKE: {C.RED}{onetake_count}{C.RESET}\n")


def print_details(accounts):
    """Печатает детали активных банов/мутов ONETAKE."""
    banned = [a for a in accounts if a["onetake"] == "Да"]
    if not banned:
        return

    print(f"{C.RED}{C.BOLD}Активные наказания ONETAKE:{C.RESET}\n")
    for a in banned:
        print(f"  {C.BOLD}[{a['steamid64']}] {a['persona']} / "
              f"{a['login']}{C.RESET}")

        info = a["onetake_info"]

        for b in info["bans"]:
            if b["active"]:
                print(f"    {C.RED}БАН{C.RESET} | {b['reason']} | "
                      f"срок: {b['duration']} | админ: {b['admin']}")

        for m in info["mutes"]:
            if m["active"]:
                print(f"    {C.YELLOW}МУТ{C.RESET} | {m['reason']} | "
                      f"срок: {m['duration']} | админ: {m['admin']}")
        print()


# ============================================================
#  МЕНЮ
# ============================================================

def menu(accounts):
    """Меню после сбора: открыть профиль или экспортировать."""
    while True:
        print(f"  {C.CYAN}1{C.RESET} — Открыть профиль ONETAKE")
        print(f"  {C.CYAN}2{C.RESET} — Экспорт на рабочий стол")
        print(f"  {C.CYAN}0{C.RESET} — Выход\n")

        try:
            choice = input("  Выбор [0]: ").strip() or "0"
        except EOFError:
            return

        if choice == "0":
            return

        if choice == "1":
            try:
                n = input("  Номер аккаунта: ").strip()
            except EOFError:
                continue

            if not n.isdigit() or not (1 <= int(n) <= len(accounts)):
                print(f"  {C.YELLOW}Нет такого номера.{C.RESET}\n")
                continue

            a = accounts[int(n) - 1]
            url = open_profile(a["steamid64"])
            if url:
                print(f"  {C.GREEN}[✓] {a['login']} → {url}{C.RESET}\n")
            else:
                print(f"  {C.RED}[!] Не удалось открыть браузер{C.RESET}\n")
            continue

        if choice == "2":
            path = os.path.join(
                get_desktop(),
                f"steam_accounts_"
                f"{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
            )
            if export_txt(accounts, path):
                print(f"  {C.GREEN}[✓] Сохранено: {path}{C.RESET}\n")
            continue

        print(f"  {C.YELLOW}Введите 0, 1 или 2.{C.RESET}\n")


# ============================================================
#  MAIN
# ============================================================

def main():
    enable_ansi()

    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW("ONETAKE Manager")
        except Exception:
            pass

    print(f"\n{C.CYAN}{C.BOLD}ONETAKE Account Manager{C.RESET}\n")

    steam_path = get_steam_path()
    print(f"  Steam: {steam_path}\n")

    userdata = os.path.join(steam_path, "userdata")
    if not os.path.isdir(userdata):
        print(f"{C.RED}  [!] Папка userdata не найдена{C.RESET}")
        input("  Enter...")
        return

    # Папки = SteamID3. Папку "0" пропускаем — это служебный кэш Steam,
    # иначе получится псевдо-ID 76561197960265728.
    account_map = parse_loginusers(steam_path)
    folders = [
        f for f in sorted(os.listdir(userdata))
        if f.isdigit()
        and f != "0"
        and os.path.isdir(os.path.join(userdata, f))
    ]

    if not folders:
        print(f"{C.YELLOW}  Аккаунтов не найдено{C.RESET}")
        input("  Enter...")
        return

    print(f"  Найдено аккаунтов: {len(folders)}. Проверка...\n")

    accounts = []
    for i, sid3 in enumerate(folders, 1):
        sid64 = steam3_to_steam64(sid3)

        print(f"  [{i}/{len(folders)}] {sid64}...", end="\r")

        info = check_onetake(sid64)
        accounts.append({
            "steamid64":    sid64,
            "login":        account_map.get(sid3, "Неизвестно"),
            "persona":      get_persona_name(steam_path, sid3),
            "vac":          check_vac(sid64),
            "onetake":      info["status"],
            "onetake_info": info,
        })

        time.sleep(0.3)  # пауза, чтобы не злить DDoS-Guard

    print(" " * 50, end="\r\n")

    print_table(accounts)
    print_details(accounts)
    menu(accounts)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Прервано.")
        sys.exit(0)