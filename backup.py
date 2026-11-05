# -*- coding: utf-8 -*-
"""
Резервные копии ботов и панели.

Зовётся из mybot, но работает и сам по себе:

    backup.py make      — собрать копию, отправить в Телеграм и на Диск
    backup.py list      — что есть на сервере
    backup.py check     — развернуть последнюю во временную папку и проверить
    backup.py clean     — убрать старые по правилу хранения
    backup.py link      — открыть/закрыть ссылку на Диске (link open|close)

ЧТО ВНУТРИ АРХИВА

    artemis/        вся папка ботов, кроме venv, кэшей, логов и копий
    panel.sql       дамп базы XMPlus
    СВЕДЕНИЯ.txt    дата, размеры, сколько клиентов и диллеров

venv не кладём сознательно: это триста мегабайт чужих библиотек,
собранных под конкретную систему. На новом сервере он создаётся заново
одной кнопкой, а в архиве только раздувал бы вес в сто раз.

КУДА ОТПРАВЛЯЕТСЯ

  1. В Телеграм — файлом, тем же ботом, которым панель шлёт свои копии.
     Токен и номер чата читаем ПРЯМО ИЗ БАЗЫ ПАНЕЛИ: сменишь бота в
     панели — копии поедут за ним сами, настраивать ничего не нужно.
     Это основной путь восстановления: файл всегда в телефоне.

  2. На Google Drive — ЗАКРЫТЫМ файлом, без публичной ссылки. Это
     запасная корзина «на всякий случай». Ссылку можно открыть
     отдельной командой и так же закрыть обратно.

ПОЧЕМУ НЕ ШИФРУЕМ. Хозяин решил не усложнять. Поэтому файл на Диске
закрыт по умолчанию: пока ссылку не открыли, скачать его может только
владелец ключа сервис-аккаунта.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta

# Пути можно переопределить средой: так их задаёт mybot, и так же
# удобно гонять проверки, не трогая настоящие папки.
ARTEMIS = os.environ.get("MYBOT_ARTEMIS") or "/root/4idea/Artemis"
КОПИИ = os.environ.get("MYBOT_BACKUPS") or "/root/backups"
ПАПКА_НА_ДИСКЕ = "Резервные копии ботов"

# Что в архив не кладём никогда.
ИСКЛЮЧИТЬ = {
    "venv", "__pycache__", "logs", ".git", "node_modules",
}
ИСКЛЮЧИТЬ_ПО_КОНЦУ = (".pyc", ".pyo", ".tmp", ".БЭКАП")

# Сколько копий держим на сервере.
СУТОЧНЫХ = 14
НЕДЕЛЬНЫХ = 4


def _печать(*a):
    print(*a, flush=True)


# ───────────────────────── доступ к панели ─────────────────────────

def _конфиг_ботов():
    """DB_CONFIG из config.py бота №1. Читаем исполнением файла в
    отдельном пространстве имён: тащить сюда весь бот ради трёх строк
    незачем."""
    путь = os.path.join(ARTEMIS, "config.py")
    if not os.path.exists(путь):
        return None
    простор = {"__name__": "config", "__file__": путь}
    try:
        with open(путь, "r", encoding="utf-8") as f:
            exec(compile(f.read(), путь, "exec"), простор)
    except Exception as e:
        _печать(f"config.py не прочитался: {e}")
        return None
    db = простор.get("DB_CONFIG")
    return db if isinstance(db, dict) else None


def _из_базы(запрос, параметры=()):
    db = _конфиг_ботов()
    if not db:
        return None
    try:
        sys.path.insert(0, ARTEMIS)
        import mysql.connector as sql
        conn = sql.connect(**db)
        cur = conn.cursor()
        cur.execute(запрос, параметры)
        строки = cur.fetchall()
        cur.close()
        conn.close()
        return строки
    except Exception as e:
        _печать(f"база панели не ответила: {e}")
        return None


def настройки_телеграма():
    """(токен, номер чата) из настроек панели. Это тот же бот, которым
    панель шлёт свои копии, — отдельный заводить незачем."""
    строки = _из_базы(
        "SELECT `key`, `value` FROM settings "
        "WHERE `key` IN ('telegramtoken','telegramchatid')")
    if not строки:
        # В разных версиях панели столбцы называются по-разному.
        строки = _из_базы(
            "SELECT name, value FROM settings "
            "WHERE name IN ('telegramtoken','telegramchatid')")
    if not строки:
        return (None, None)
    d = {str(k): str(v) for k, v in строки}
    return (d.get("telegramtoken") or None, d.get("telegramchatid") or None)


# ───────────────────────── дамп базы ─────────────────────────

def _найти_mysqldump():
    """aaPanel кладёт mysqldump к себе, а не в общий PATH."""
    из_пути = shutil.which("mysqldump")
    if из_пути:
        return из_пути
    import glob
    for шаблон in ("/www/server/mysql/bin/mysqldump",
                   "/www/server/mysql*/bin/mysqldump",
                   "/usr/local/mysql/bin/mysqldump"):
        найдено = glob.glob(шаблон)
        if найдено:
            return найдено[0]
    return None


def дамп_базы(куда: str) -> tuple:
    """Снять дамп. → (получилось, причина)."""
    db = _конфиг_ботов()
    if not db:
        return (False, "в config.py нет DB_CONFIG")
    бинарь = _найти_mysqldump()
    if not бинарь:
        return (False, "не нашёл mysqldump — ни в PATH, ни у aaPanel")

    команда = [
        бинарь,
        f"--host={db.get('host', 'localhost')}",
        f"--user={db.get('user', 'root')}",
        f"--port={int(db.get('port', 3306))}",
        # Снимок без блокировки таблиц: панель и боты в это время
        # продолжают работать, как будто ничего не происходит.
        "--single-transaction", "--quick", "--routines", "--events",
        "--default-character-set=utf8mb4",
        str(db.get("database", "")),
    ]
    среда = dict(os.environ)
    # Пароль через переменную среды, а не в командной строке: иначе он
    # виден любому, кто сделает ps на сервере.
    среда["MYSQL_PWD"] = str(db.get("password", ""))

    try:
        with open(куда, "wb") as f:
            res = subprocess.run(команда, stdout=f, stderr=subprocess.PIPE,
                                 env=среда, timeout=900)
    except subprocess.TimeoutExpired:
        return (False, "mysqldump не уложился в 15 минут")
    except Exception as e:
        return (False, f"mysqldump не запустился: {e}")

    if res.returncode != 0:
        хвост = (res.stderr or b"").decode(errors="ignore")[-300:]
        return (False, f"mysqldump вернул ошибку: {хвост}")
    if os.path.getsize(куда) < 1024:
        return (False, "дамп получился пустым")
    return (True, "")


# ───────────────────────── сборка архива ─────────────────────────

def _пропустить(путь: str) -> bool:
    имя = os.path.basename(путь)
    if имя in ИСКЛЮЧИТЬ:
        return True
    if имя.endswith(ИСКЛЮЧИТЬ_ПО_КОНЦУ):
        return True
    if ".БЭКАП" in имя or ".bak" in имя:
        return True
    return False


def _сведения(дамп: str) -> str:
    клиентов = диллеров = "?"
    строки = _из_базы("SELECT COUNT(*) FROM user")
    if строки:
        клиентов = строки[0][0]
    try:
        with open(os.path.join(ARTEMIS, "dillerbot", "dealers.json"),
                  encoding="utf-8") as f:
            диллеров = len(json.load(f) or {})
    except Exception:
        диллеров = "нет файла"
    return (
        "РЕЗЕРВНАЯ КОПИЯ\n"
        "═══════════════════════════════════\n"
        f"Создана:    {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
        f"Клиентов:   {клиентов}\n"
        f"Диллеров:   {диллеров}\n"
        f"Дамп базы:  {os.path.getsize(дамп) / 1048576:.1f} МБ\n"
        f"Питон:      {sys.version.split()[0]}\n"
        "\n"
        "Внутри:\n"
        "  artemis/     папка ботов (без venv, кэшей и логов)\n"
        "  panel.sql    дамп базы XMPlus\n"
        "\n"
        "Как развернуть:\n"
        "  поставить mybot с GitHub, затем\n"
        "  mybot → Резервные копии → Восстановить\n"
    )


def собрать() -> tuple:
    """Собрать архив. → (путь, причина)."""
    if not os.path.isdir(ARTEMIS):
        return (None, f"нет папки {ARTEMIS}")
    os.makedirs(КОПИИ, exist_ok=True)

    метка = datetime.now().strftime("%Y-%m-%d-%H%M")
    архив = os.path.join(КОПИИ, f"artemis-{метка}.tar.gz")
    врем = tempfile.mkdtemp(prefix="backup_")
    дамп = os.path.join(врем, "panel.sql")

    try:
        _печать("Снимаю дамп базы…")
        ok, почему = дамп_базы(дамп)
        if not ok:
            return (None, почему)
        _печать(f"  дамп: {os.path.getsize(дамп) / 1048576:.1f} МБ")

        сведения = os.path.join(врем, "СВЕДЕНИЯ.txt")
        with open(сведения, "w", encoding="utf-8") as f:
            f.write(_сведения(дамп))

        _печать("Складываю архив…")
        with tarfile.open(архив, "w:gz", compresslevel=9) as t:
            t.add(ARTEMIS, arcname="artemis",
                  filter=lambda ti: None if _пропустить(ti.name) else ti)
            t.add(дамп, arcname="panel.sql")
            t.add(сведения, arcname="СВЕДЕНИЯ.txt")
    except Exception as e:
        try:
            os.unlink(архив)
        except Exception:
            pass
        return (None, f"архив не собрался: {e}")
    finally:
        shutil.rmtree(врем, ignore_errors=True)

    _печать(f"  архив: {os.path.getsize(архив) / 1048576:.1f} МБ")
    return (архив, "")


# ───────────────────────── хранение ─────────────────────────

def список() -> list:
    """Копии на сервере, свежие сверху."""
    if not os.path.isdir(КОПИИ):
        return []
    out = []
    for имя in os.listdir(КОПИИ):
        if not (имя.startswith("artemis-") and имя.endswith(".tar.gz")):
            continue
        путь = os.path.join(КОПИИ, имя)
        out.append({"имя": имя, "путь": путь,
                    "размер": os.path.getsize(путь),
                    "когда": os.path.getmtime(путь)})
    out.sort(key=lambda x: -x["когда"])
    return out


def убрать_старые() -> int:
    """Оставляем 14 суточных и 4 недельных. Остальное удаляем.

    Недельной считается самая свежая копия каждой недели. Так история
    за месяц остаётся, а места занимает всего ничего.
    """
    все = список()
    if len(все) <= СУТОЧНЫХ:
        return 0

    оставить = {к["путь"] for к in все[:СУТОЧНЫХ]}
    недели: dict = {}
    for к in все:
        неделя = datetime.fromtimestamp(к["когда"]).strftime("%Y-%W")
        if неделя not in недели:
            недели[неделя] = к["путь"]
    for путь in list(недели.values())[:НЕДЕЛЬНЫХ]:
        оставить.add(путь)

    убрано = 0
    for к in все:
        if к["путь"] in оставить:
            continue
        try:
            os.unlink(к["путь"])
            убрано += 1
        except Exception:
            pass
    return убрано


# ───────────────────────── отправка в Телеграм ─────────────────────────

def в_телеграм(архив: str) -> tuple:
    """Отправить файл себе в Телеграм. → (получилось, причина).

    Берём бота панели: он уже настроен, уже шлёт тебе копии базы, и
    отдельный заводить незачем. Если в панели его выключили или сменили
    — здесь это сразу станет видно.
    """
    токен, чат = настройки_телеграма()
    if not токен or not чат:
        return (False, "в настройках панели нет токена или номера чата")

    размер = os.path.getsize(архив)
    # Телеграм берёт у ботов файлы до 50 МБ. Наш архив на порядок
    # меньше, но если однажды разрастётся — пусть скажет прямо, а не
    # роняет непонятную ошибку.
    if размер > 49 * 1024 * 1024:
        return (False, f"архив {размер / 1048576:.0f} МБ — больше 50 МБ, "
                       f"Телеграм столько у ботов не берёт")

    sys.path.insert(0, ARTEMIS)
    try:
        import requests
    except Exception as e:
        return (False, f"нет библиотеки requests: {e}")

    подпись = (f"🗄 Копия ботов\n"
               f"{datetime.now().strftime('%d.%m.%Y %H:%M')} · "
               f"{размер / 1048576:.1f} МБ")

    # Прокси для Телеграма: на сервере он может быть обязателен.
    прокси = {}
    for ключ in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if os.environ.get(ключ):
            прокси = {"https": os.environ[ключ], "http": os.environ[ключ]}
            break
    if not прокси:
        прокси = _прокси_из_env_файла()

    try:
        with open(архив, "rb") as f:
            res = requests.post(
                f"https://api.telegram.org/bot{токен}/sendDocument",
                data={"chat_id": чат, "caption": подпись},
                files={"document": (os.path.basename(архив), f)},
                proxies=прокси or None, timeout=300)
    except Exception as e:
        return (False, f"не отправилось: {e}")

    try:
        ответ = res.json()
    except Exception:
        return (False, f"Телеграм ответил непонятно (код {res.status_code})")
    if not ответ.get("ok"):
        return (False, f"Телеграм отказал: {ответ.get('description')}")
    return (True, "")


def _прокси_из_env_файла():
    """Прокси из .env бота №2, если в среде его нет.

    У ботов прокси прописан в .env, а скрипт запускается из cron, где
    никакой среды нет вовсе. Без этого копия просто не улетит, и понять
    почему будет непросто.
    """
    путь = os.path.join(ARTEMIS, "azureipauto", ".env")
    if not os.path.exists(путь):
        return {}
    данные = {}
    try:
        with open(путь, encoding="utf-8") as f:
            for строка in f:
                строка = строка.strip()
                if not строка or строка.startswith("#") or "=" not in строка:
                    continue
                k, v = строка.split("=", 1)
                данные[k.strip()] = v.strip()
    except Exception:
        return {}
    хост, порт = данные.get("PROXY_HOST"), данные.get("PROXY_PORT")
    if not хост or not порт:
        return {}
    тип = (данные.get("PROXY_TYPE") or "http").lower()
    поль, пароль = данные.get("PROXY_USER"), данные.get("PROXY_PASS")
    вход = ""
    if поль:
        from urllib.parse import quote
        вход = quote(поль, safe="")
        if пароль:
            вход += ":" + quote(пароль, safe="")
        вход += "@"
    адрес = f"{тип}://{вход}{хост}:{порт}"
    return {"https": адрес, "http": адрес}


# ───────────────────────── Google Drive ─────────────────────────

def _диск():
    """Клиент Drive тем же ключом, которым бот заливает подписки."""
    sys.path.insert(0, ARTEMIS)
    try:
        from googledrive import _build_drive_service
        return _build_drive_service()
    except Exception as e:
        _печать(f"Диск недоступен: {e}")
        return None


def _папка_копий(service) -> str | None:
    """Найти или завести папку для копий. Возвращает её id."""
    try:
        q = (f"name='{ПАПКА_НА_ДИСКЕ}' and "
             f"mimeType='application/vnd.google-apps.folder' and trashed=false")
        res = service.files().list(q=q, fields="files(id)").execute()
        файлы = res.get("files") or []
        if файлы:
            return файлы[0]["id"]
        создана = service.files().create(
            body={"name": ПАПКА_НА_ДИСКЕ,
                  "mimeType": "application/vnd.google-apps.folder"},
            fields="id").execute()
        return создана.get("id")
    except Exception as e:
        _печать(f"папка на Диске не завелась: {e}")
        return None


def на_диск(архив: str) -> tuple:
    """Залить архив на Диск ЗАКРЫТЫМ файлом. → (id файла, причина).

    Публичных прав не ставим сознательно: внутри токены и ключи. Ссылку
    открывают отдельной командой, когда она понадобится.
    """
    service = _диск()
    if service is None:
        return (None, "нет доступа к Google Drive")
    папка = _папка_копий(service)
    try:
        from googleapiclient.http import MediaFileUpload
        носитель = MediaFileUpload(архив, mimetype="application/gzip",
                                   resumable=False)
        тело = {"name": os.path.basename(архив)}
        if папка:
            тело["parents"] = [папка]
        файл = service.files().create(body=тело, media_body=носитель,
                                      fields="id").execute()
        return (файл.get("id"), "")
    except Exception as e:
        return (None, f"не залилось: {e}")


def ссылка_открыть(file_id: str) -> tuple:
    """Открыть доступ по ссылке. → (ссылка, причина)."""
    service = _диск()
    if service is None:
        return (None, "нет доступа к Google Drive")
    try:
        service.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}).execute()
        return (f"https://drive.google.com/uc?id={file_id}&export=download", "")
    except Exception as e:
        return (None, f"не открылось: {e}")


def ссылка_закрыть(file_id: str) -> tuple:
    """Закрыть доступ обратно. → (получилось, причина)."""
    service = _диск()
    if service is None:
        return (False, "нет доступа к Google Drive")
    try:
        права = service.permissions().list(
            fileId=file_id, fields="permissions(id,type)").execute()
        убрано = 0
        for п in права.get("permissions") or []:
            if п.get("type") == "anyone":
                service.permissions().delete(fileId=file_id,
                                             permissionId=п["id"]).execute()
                убрано += 1
        return (True, "" if убрано else "публичного доступа и не было")
    except Exception as e:
        return (False, f"не закрылось: {e}")


def копии_на_диске() -> list:
    """Что лежит на Диске: [{id, имя, размер, открыт}]."""
    service = _диск()
    if service is None:
        return []
    папка = _папка_копий(service)
    if not папка:
        return []
    try:
        res = service.files().list(
            q=f"'{папка}' in parents and trashed=false",
            orderBy="createdTime desc", pageSize=50,
            fields="files(id,name,size,createdTime,permissions(type))"
        ).execute()
        out = []
        for f in res.get("files") or []:
            открыт = any(п.get("type") == "anyone"
                         for п in (f.get("permissions") or []))
            out.append({"id": f["id"], "имя": f["name"],
                        "размер": int(f.get("size") or 0),
                        "когда": f.get("createdTime", "")[:16].replace("T", " "),
                        "открыт": открыт})
        return out
    except Exception as e:
        _печать(f"список на Диске не прочитался: {e}")
        return []


# ───────────────────────── проверка копии ─────────────────────────

def проверить(архив: str = None) -> tuple:
    """Развернуть копию во временную папку и убедиться, что она целая.

    КОПИЯ, КОТОРУЮ НИ РАЗУ НЕ РАЗВОРАЧИВАЛИ, — ЭТО НЕ КОПИЯ, А НАДЕЖДА.
    Поэтому проверяем: архив открывается, дамп на месте и внутри него
    есть таблица клиентов, а json-файлы ботов читаются.

    → (всё_хорошо, отчёт строкой)
    """
    if архив is None:
        все = список()
        if not все:
            return (False, "копий на сервере нет")
        архив = все[0]["путь"]

    замечания = []
    врем = tempfile.mkdtemp(prefix="check_")
    try:
        try:
            with tarfile.open(архив, "r:gz") as t:
                имена = t.getnames()
                t.extractall(врем)
        except Exception as e:
            return (False, f"архив не открылся: {e}")

        дамп = os.path.join(врем, "panel.sql")
        if not os.path.exists(дамп):
            замечания.append("нет panel.sql")
        else:
            размер = os.path.getsize(дамп)
            with open(дамп, "r", encoding="utf-8", errors="ignore") as f:
                голова = f.read(200000)
            if "CREATE TABLE" not in голова:
                замечания.append("в дампе нет ни одной таблицы")
            if "`user`" not in голова:
                замечания.append("в дампе не видно таблицы клиентов")
            if размер < 100000:
                замечания.append(f"дамп подозрительно мал: {размер} байт")

        папка = os.path.join(врем, "artemis")
        if not os.path.isdir(папка):
            замечания.append("нет папки artemis")
        else:
            обязательные = ["config.py", "main.py", "telegram_bot.py"]
            для_проверки = ["other_config", "azureipauto/data.json",
                            "dillerbot/dealers.json"]
            for имя in обязательные:
                if not os.path.exists(os.path.join(папка, имя)):
                    замечания.append(f"нет {имя}")
            for имя in для_проверки:
                путь = os.path.join(папка, имя)
                if not os.path.exists(путь):
                    замечания.append(f"нет {имя} (может быть и нормально)")
            # Все json должны читаться: битый файл в копии хуже, чем его
            # отсутствие — он создаёт ложное спокойствие.
            битые = []
            for корень, _, файлы in os.walk(папка):
                for ф in файлы:
                    if not ф.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(корень, ф), encoding="utf-8") as g:
                            json.load(g)
                    except Exception:
                        битые.append(ф)
            if битые:
                замечания.append("не читаются json: " + ", ".join(битые[:5]))

        размер_архива = os.path.getsize(архив) / 1048576
        отчёт = [f"Проверена: {os.path.basename(архив)}",
                 f"Размер: {размер_архива:.1f} МБ, файлов внутри: {len(имена)}"]
        if замечания:
            отчёт.append("")
            отчёт.append("Замечания:")
            отчёт.extend("  • " + з for з in замечания)
        else:
            отчёт.append("Всё на месте — копия разворачивается.")
        серьёзные = [з for з in замечания if "может быть и нормально" not in з]
        return (not серьёзные, "\n".join(отчёт))
    finally:
        shutil.rmtree(врем, ignore_errors=True)


# ───────────────────────── всё вместе ─────────────────────────

def сделать() -> tuple:
    """Собрать копию и разослать. → (получилось, отчёт строкой)."""
    начало = time.time()
    архив, почему = собрать()
    if not архив:
        return (False, f"🔴 Копия не собралась: {почему}")

    строки = [f"✅ Архив: {os.path.basename(архив)} "
              f"({os.path.getsize(архив) / 1048576:.1f} МБ)"]

    _печать("Отправляю в Телеграм…")
    ok, почему = в_телеграм(архив)
    строки.append("✅ Отправлено в Телеграм" if ok
                  else f"⚠️ Телеграм: {почему}")

    _печать("Заливаю на Диск…")
    fid, почему = на_диск(архив)
    строки.append(f"✅ На Диске (закрытым файлом)" if fid
                  else f"⚠️ Диск: {почему}")

    убрано = убрать_старые()
    if убрано:
        строки.append(f"🧹 Убрал старых копий: {убрано}")

    строки.append(f"⏱ Заняло {int(time.time() - начало)} с")
    # Телеграм — основной путь. Если не ушло туда, считаем неудачей,
    # даже когда архив лёг на Диск: до Диска ещё надо добраться.
    return (ok, "\n".join(строки))


def _команда_строкой(args) -> int:
    что = (args[0] if args else "make").lower()

    if что in ("make", "сделать"):
        ok, отчёт = сделать()
        _печать("")
        _печать(отчёт)
        return 0 if ok else 1

    if что in ("list", "список"):
        все = список()
        if not все:
            _печать("Копий на сервере нет.")
            return 0
        for к in все:
            когда = datetime.fromtimestamp(к["когда"]).strftime("%d.%m.%Y %H:%M")
            _печать(f"  {когда}   {к['размер'] / 1048576:6.1f} МБ   {к['имя']}")
        return 0

    if что in ("check", "проверить"):
        ok, отчёт = проверить(args[1] if len(args) > 1 else None)
        _печать(отчёт)
        return 0 if ok else 1

    if что in ("clean", "убрать"):
        _печать(f"Убрано копий: {убрать_старые()}")
        return 0

    if что == "drive":
        для = копии_на_диске()
        if not для:
            _печать("На Диске копий нет (или Диск недоступен).")
            return 0
        for к in для:
            замок = "🔓 открыт" if к["открыт"] else "🔒 закрыт"
            _печать(f"  {к['когда']}  {к['размер'] / 1048576:6.1f} МБ  "
                    f"{замок}  {к['имя']}  id={к['id']}")
        return 0

    if что == "link":
        действие = (args[1] if len(args) > 1 else "").lower()
        fid = args[2] if len(args) > 2 else None
        if not fid:
            для = копии_на_диске()
            if not для:
                _печать("На Диске копий нет.")
                return 1
            fid = для[0]["id"]
        if действие in ("open", "открыть"):
            ссылка, почему = ссылка_открыть(fid)
            _печать(ссылка or f"не вышло: {почему}")
            return 0 if ссылка else 1
        if действие in ("close", "закрыть"):
            ok, почему = ссылка_закрыть(fid)
            _печать("Доступ закрыт." if ok else f"не вышло: {почему}")
            return 0 if ok else 1
        _печать("Нужно: link open | link close")
        return 1

    _печать(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(_команда_строкой(sys.argv[1:]))
