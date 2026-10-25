"""Telegram-бот-напоминалка для финансов (без распознавания фото).

- каждый день в 14:00 напоминает внести доходы и траты;
- напоминает об оплатах (аренда, связь, долги и т. д.);
- предупреждает о слишком больших тратах и превышении лимитов;
- принимает записи текстом («120 кофе», «+300 чаевые») и выгружает CSV для приложения.
"""
import calendar
import csv
import io
import logging
import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER = int(os.environ["ALLOWED_USER_ID"])  # бот отвечает только этому пользователю
CUR = os.environ.get("CURRENCY", "лей")
DB = os.environ.get("DB_PATH", "finance.db")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Chisinau"))
DEFAULT_TIME = os.environ.get("REMIND_TIME", "14:00")
MORNING = time(9, 0, tzinfo=TZ)  # утренний сигнал, только если есть что оплатить

logging.basicConfig(level=logging.INFO)

KW = [
    ("Кафе и доставка", r"bolt food|cafe|кафе|coffee|кофе|restaur|ресторан|pizza|пицц|burger|mcdonald|kfc|glovo|wolt|sushi|суши"),
    ("Транспорт", r"bolt|uber|taxi|такси|petrom|lukoil|azs|fuel|топлив|бензин|автобус|троллейбус|parking|парковк|проезд"),
    ("Еда", r"market|магазин|супермаркет|linella|kaufland|fidesco|nr1|продукт|\bеда\b|хлеб|молоко"),
    ("Связь", r"orange|moldcell|unite|телефон|связь|интернет|moldtelecom|starnet"),
    ("Подписки", r"netflix|spotify|youtube|apple|icloud|google|подписк|chatgpt"),
    ("Курение", r"tutun|tobacco|табак|iqos|сигарет|курен"),
    ("Донат", r"donat|донат|patreon|twitch|boosty"),
    ("Здоровье", r"farmac|pharm|аптек|clinic|клиник|врач|стомат"),
    ("Одежда", r"zara|h&m|одежд|shoes|обувь|куртк|футболк"),
    ("Развлечения", r"cinema|кино|steam|playstation|game|игр|concert|театр|club|клуб"),
    ("Жильё", r"rent|аренд|жкх|коммун|gaz|\bгаз\b|электр|квартир"),
]


def category(text):
    t = text.lower()
    for k, p in KW:
        if re.search(p, t):
            return k
    return "Другое"


def parse_entry(text):
    """'120 кофе' -> ('out','Кафе и доставка',120.0,'кофе'); '+300 чаевые' -> ('in','Чаевые',300.0,'')."""
    m = re.search(r"([+\-\u2212])?\s*(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    amount = float(m.group(2).replace(",", "."))
    if amount <= 0:
        return None
    desc = (text[:m.start()] + " " + text[m.end():]).strip()
    inc = m.group(1) == "+" or re.search(r"чаев|ставк|процент|продаж|зарплат", desc, re.I)
    if inc:
        k = ("Чаевые" if re.search(r"чаев", desc, re.I)
             else "Проценты с продаж" if re.search(r"процент|продаж", desc, re.I) else "Ставка")
        return "in", k, amount, ""
    return "out", category(desc), amount, desc[:60]


def parse_date(s):
    today = now().date()
    for f in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", s)
    if m:
        try:
            d = date(today.year, int(m.group(2)), int(m.group(1)))
            return d if d >= today else d.replace(year=today.year + 1)
        except ValueError:
            return None
    return None


def add_month(d):
    y, m = d.year + (d.month == 12), d.month % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def now():
    return datetime.now(TZ)


def fm(n):
    return f"{round(n):,}".replace(",", "\u00a0") + f" {CUR}"


# ---------- база ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS e(id INTEGER PRIMARY KEY, d TEXT, t TEXT, k TEXT, a REAL, n TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS p(id INTEGER PRIMARY KEY, name TEXT, a REAL, due TEXT, rep INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS s(k TEXT PRIMARY KEY, v TEXT)")


def get(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM s WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def put(k, v):
    with db() as c:
        c.execute("INSERT INTO s(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def spent(a, b):
    with db() as c:
        return c.execute("SELECT COALESCE(SUM(a),0) FROM e WHERE t='out' AND k!='Долги' AND d BETWEEN ? AND ?",
                         (a.isoformat(), b.isoformat())).fetchone()[0]


# ---------- предупреждения о тратах ----------
def alerts(amount, label, d):
    L = []
    big = float(get("single", 0))
    if big and amount >= big:
        L.append(f"Крупная трата: {fm(amount)} ({label}).")
    day_lim = float(get("day", 0))
    if day_lim:
        after = spent(d, d)
        if after - amount < day_lim <= after:
            L.append(f"Дневной лимит {fm(day_lim)} превышен: потрачено {fm(after)}.")
    mon_lim = float(get("month", 0))
    if mon_lim:
        a = d.replace(day=1)
        b = a.replace(day=calendar.monthrange(a.year, a.month)[1])
        after = spent(a, b)
        before = after - amount
        if before < mon_lim <= after:
            L.append(f"Месячный лимит {fm(mon_lim)} превышен: потрачено {fm(after)}.")
        elif before < mon_lim * 0.8 <= after:
            L.append(f"Потрачено 80% месячного лимита: {fm(after)} из {fm(mon_lim)}.")
    return L


# ---------- платежи ----------
def due_lines(days):
    today = now().date()
    with db() as c:
        rs = c.execute("SELECT * FROM p ORDER BY due").fetchall()
    L = []
    for r in rs:
        k = (date.fromisoformat(r["due"]) - today).days
        if k > days:
            continue
        when = f"просрочено на {-k} дн." if k < 0 else "сегодня" if k == 0 else f"через {k} дн."
        L.append(f"• {r['name']}: {fm(r['a'])}, {when}")
    return L


async def daily(ctx: ContextTypes.DEFAULT_TYPE):
    today = now().date().isoformat()
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM e WHERE d=?", (today,)).fetchone()[0]
    L = ["Пора внести доходы и траты за день. Напишите, например: «120 кофе» или «+300 чаевые»."
         if not n else f"Сегодня записей: {n}. Если что-то забыли, добавьте."]
    due = due_lines(3)
    if due:
        L += ["", "Скоро нужно оплатить:"] + due
    await ctx.bot.send_message(OWNER, "\n".join(L))


async def morning(ctx: ContextTypes.DEFAULT_TYPE):
    due = due_lines(0)
    if due:
        await ctx.bot.send_message(OWNER, "Сегодня нужно оплатить:\n" + "\n".join(due))


# ---------- обработчики ----------
def ok(update: Update):
    return update.effective_user is not None and update.effective_user.id == OWNER


HELP = (
    "Напишите запись текстом: «120 кофе» (расход) или «+300 чаевые» (доход).\n\n"
    "/pay Аренда 4000 05.11 monthly — добавить оплату (monthly = каждый месяц)\n"
    "/payments — список оплат\n"
    "/limit single 1000 — предупреждать о трате от этой суммы\n"
    "/limit day 1500 — дневной лимит\n"
    "/limit month 20000 — месячный лимит (0 выключает)\n"
    "/time 14:00 — время ежедневного напоминания\n"
    "/last — последние записи\n"
    "/csv — выгрузка для приложения (/csv 2026-10 за месяц)")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ok(update):
        await update.message.reply_text(HELP)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    e = parse_entry(update.message.text)
    if not e:
        await update.message.reply_text("Нужна сумма. Например: «120 кофе» или «+300 чаевые».")
        return
    t, k, a, n = e
    d = now().date()
    with db() as c:
        i = c.execute("INSERT INTO e(d,t,k,a,n) VALUES(?,?,?,?,?)", (d.isoformat(), t, k, a, n)).lastrowid
    text = f"Записано: {k} · {'+' if t == 'in' else '−'}{fm(a)}" + (f" ({n})" if n else "")
    if t == "out":
        text += "".join("\n\n" + x for x in alerts(a, n or k, d))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Удалить", callback_data=f"del:{i}")]])
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_limit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    names = {"single": "одна трата", "day": "в день", "month": "в месяц"}
    a = ctx.args
    if len(a) == 2 and a[0] in names and re.fullmatch(r"\d+(?:[.,]\d+)?", a[1]):
        put(a[0], float(a[1].replace(",", ".")))
    elif a:
        await update.message.reply_text("Пример: /limit day 1500. Виды: single, day, month.")
        return
    L = [f"{v}: {fm(float(get(k, 0)))}" if float(get(k, 0)) else f"{v}: выключено" for k, v in names.items()]
    await update.message.reply_text("Лимиты:\n" + "\n".join(L))


async def cmd_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", ctx.args[0]) if ctx.args else None
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        await update.message.reply_text(f"Пример: /time 14:00. Сейчас: {get('time', DEFAULT_TIME)}.")
        return
    put("time", f"{int(m.group(1)):02d}:{m.group(2)}")
    for j in ctx.job_queue.get_jobs_by_name("daily"):
        j.schedule_removal()
    ctx.job_queue.run_daily(daily, time(int(m.group(1)), int(m.group(2)), tzinfo=TZ), name="daily")
    await update.message.reply_text(f"Напоминание каждый день в {get('time')}.")


async def cmd_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    tokens = list(ctx.args)
    rep = any(t.lower() in ("monthly", "мес", "ежемесячно") for t in tokens)
    tokens = [t for t in tokens if t.lower() not in ("monthly", "мес", "ежемесячно")]
    di = next((i for i, t in enumerate(tokens) if parse_date(t)), None)
    due = parse_date(tokens.pop(di)) if di is not None else None
    ai = next((i for i, t in enumerate(tokens) if re.fullmatch(r"\d+(?:[.,]\d+)?", t)), None)
    amount = float(tokens.pop(ai).replace(",", ".")) if ai is not None else None
    name = " ".join(tokens).strip()
    if not due or not amount or not name:
        await update.message.reply_text("Пример: /pay Аренда 4000 05.11 monthly\n(название, сумма, дата, при желании monthly)")
        return
    with db() as c:
        c.execute("INSERT INTO p(name,a,due,rep) VALUES(?,?,?,?)", (name[:40], amount, due.isoformat(), int(rep)))
    await update.message.reply_text(f"Добавлено: {name}, {fm(amount)}, до {due.strftime('%d.%m.%Y')}"
                                    + (", каждый месяц" if rep else "") + ".")


async def cmd_payments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    with db() as c:
        rs = c.execute("SELECT * FROM p ORDER BY due").fetchall()
    if not rs:
        await update.message.reply_text("Оплат пока нет. Добавьте: /pay Аренда 4000 05.11 monthly")
    for r in rs:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Оплачено", callback_data=f"paid:{r['id']}"),
                                    InlineKeyboardButton("Удалить", callback_data=f"pdel:{r['id']}")]])
        await update.message.reply_text(
            f"{r['name']} · {fm(r['a'])} · до {date.fromisoformat(r['due']).strftime('%d.%m.%Y')}"
            + (" · каждый месяц" if r["rep"] else ""), reply_markup=kb)


async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    with db() as c:
        rs = c.execute("SELECT * FROM e ORDER BY id DESC LIMIT 5").fetchall()
    if not rs:
        await update.message.reply_text("Записей пока нет.")
    for r in rs:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Удалить", callback_data=f"del:{r['id']}")]])
        await update.message.reply_text(
            f"{r['d']} · {r['k']} · {'+' if r['t'] == 'in' else '−'}{fm(r['a'])} {r['n']}".strip(), reply_markup=kb)


async def cmd_csv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return
    q, p = "SELECT * FROM e", ()
    if ctx.args and re.fullmatch(r"\d{4}-\d{2}", ctx.args[0]):
        q, p = q + " WHERE d LIKE ?", (ctx.args[0] + "%",)
    with db() as c:
        rs = c.execute(q + " ORDER BY d, id", p).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Сумма", "Описание"])
    for r in rs:
        w.writerow([r["d"], f"{(-r['a'] if r['t'] == 'out' else r['a']):.2f}", f"[{r['k']}] {r['n']}".strip()])
    await update.message.reply_document(io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename="rashody.csv",
                                        caption="В приложении: День → Импорт выписки.")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    act, _, i = q.data.partition(":")
    with db() as c:
        if act == "del":
            c.execute("DELETE FROM e WHERE id=?", (i,))
            text = "Удалено."
        elif act == "pdel":
            c.execute("DELETE FROM p WHERE id=?", (i,))
            text = "Удалено."
        elif act == "paid":
            r = c.execute("SELECT * FROM p WHERE id=?", (i,)).fetchone()
            if not r:
                text = "Уже обработано."
            elif r["rep"]:
                nd = add_month(date.fromisoformat(r["due"]))
                c.execute("UPDATE p SET due=? WHERE id=?", (nd.isoformat(), i))
                text = f"Оплачено: {r['name']}. Следующая оплата {nd.strftime('%d.%m.%Y')}."
            else:
                c.execute("DELETE FROM p WHERE id=?", (i,))
                text = f"Оплачено: {r['name']}."
        else:
            return
    await q.answer()
    await q.edit_message_text(text)


def main():
    init()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("limit", cmd_limit))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("pay", cmd_pay))
    app.add_handler(CommandHandler("payments", cmd_payments))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("csv", cmd_csv))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^(del|pdel|paid):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    h, m = map(int, get("time", DEFAULT_TIME).split(":"))
    app.job_queue.run_daily(daily, time(h, m, tzinfo=TZ), name="daily")
    app.job_queue.run_daily(morning, MORNING, name="morning")
    app.run_polling()


if __name__ == "__main__":
    main()
