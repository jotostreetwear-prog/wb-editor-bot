"""
Битрикс24 чат-бот для массового редактирования карточек Wildberries
"""

import os
import json
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# ─────────────────────────────────────────────
# Конфиг (задай через переменные окружения)
# ─────────────────────────────────────────────
BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK")   # https://ваш.bitrix24.ru/rest/1/xxxxx/
WB_TOKEN       = os.getenv("WB_TOKEN")         # токен WB Seller API
BOT_ID         = os.getenv("BITRIX_BOT_ID")    # ID бота в Битрикс24

# ─────────────────────────────────────────────
# Хранилище сессий (в памяти; замени на Redis для продакшена)
# ─────────────────────────────────────────────
# sessions[user_id] = {
#   "step": str,
#   "artikuls": list[str],
#   "field": str,
# }
sessions: dict[str, dict] = {}

FIELDS = {
    "1": ("seo_name",   "📝 Название (SEO)"),
    "2": ("seo_desc",   "📄 Описание (SEO)"),
    "3": ("category",   "📂 Категория"),
    "4": ("vendor_code","🔢 Артикул (вендорский код)"),
    "5": ("chars",      "🎨 Характеристики (цвет, размер и т.д.)"),
}

# ─────────────────────────────────────────────
# Битрикс24 — отправка сообщения
# ─────────────────────────────────────────────
async def send_message(dialog_id: str, text: str):
    url = f"{BITRIX_WEBHOOK}imbot.message.add"
    payload = {
        "BOT_ID":    BOT_ID,
        "DIALOG_ID": dialog_id,
        "MESSAGE":   text,
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload)
        logger.info("send_message → %s", r.text)

# ─────────────────────────────────────────────
# WB API — получить карточку по артикулу
# ─────────────────────────────────────────────
async def wb_get_card(vendor_code: str) -> dict | None:
    url = "https://content-api.wildberries.ru/content/v2/get/cards/list"
    headers = {"Authorization": WB_TOKEN}
    body = {
        "settings": {
            "cursor": {"limit": 1},
            "filter": {"textSearch": vendor_code, "withPhoto": -1},
        }
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=body)
        data = r.json()
        cards = data.get("cards", [])
        for c in cards:
            if c.get("vendorCode") == vendor_code:
                return c
    return None

# ─────────────────────────────────────────────
# WB API — обновить карточку
# ─────────────────────────────────────────────
async def wb_update_card(card: dict) -> bool:
    url = "https://content-api.wildberries.ru/content/v2/cards/update"
    headers = {"Authorization": WB_TOKEN}
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=[card])
        return r.status_code == 200

# ─────────────────────────────────────────────
# Применить изменение к карточке
# ─────────────────────────────────────────────
def apply_field(card: dict, field: str, value: str) -> dict:
    if field == "seo_name":
        card["title"] = value
    elif field == "seo_desc":
        card["description"] = value
    elif field == "category":
        card["subjectName"] = value
    elif field == "vendor_code":
        card["vendorCode"] = value
    elif field == "chars":
        # value формат: "Цвет:Красный, Размер:XL"
        pairs = [p.strip() for p in value.split(",")]
        new_chars = []
        for p in pairs:
            if ":" in p:
                name, val = p.split(":", 1)
                new_chars.append({"name": name.strip(), "value": val.strip()})
        existing = {c["name"]: i for i, c in enumerate(card.get("characteristics", []))}
        for nc in new_chars:
            if nc["name"] in existing:
                card["characteristics"][existing[nc["name"]]]["value"] = nc["value"]
            else:
                card.setdefault("characteristics", []).append(nc)
    return card

# ─────────────────────────────────────────────
# Меню выбора поля
# ─────────────────────────────────────────────
def field_menu() -> str:
    lines = ["Что хочешь изменить?\n"]
    for k, (_, label) in FIELDS.items():
        lines.append(f"{k}. {label}")
    lines.append("\n0. ✅ Готово (завершить сессию)")
    return "\n".join(lines)

# ─────────────────────────────────────────────
# Основная логика обработки сообщения
# ─────────────────────────────────────────────
async def handle_message(user_id: str, dialog_id: str, text: str):
    text = text.strip()
    session = sessions.get(user_id, {})
    step = session.get("step", "idle")

    # ── Старт ──────────────────────────────────
    if text.lower() in ("редактировать", "edit", "/start", "старт"):
        sessions[user_id] = {"step": "await_artikuls"}
        await send_message(dialog_id,
            "✏️ *Режим редактирования карточек WB*\n\n"
            "Введи артикулы через запятую:\n"
            "Пример: J05001, J05002, J05003"
        )
        return

    # ── Ввод артикулов ─────────────────────────
    if step == "await_artikuls":
        artikuls = [a.strip() for a in text.split(",") if a.strip()]
        if not artikuls:
            await send_message(dialog_id, "⚠️ Не распознал артикулы. Попробуй ещё раз.")
            return
        sessions[user_id] = {"step": "await_field", "artikuls": artikuls}
        await send_message(dialog_id,
            f"✅ Выбрано артикулов: {len(artikuls)}\n"
            f"({', '.join(artikuls)})\n\n" + field_menu()
        )
        return

    # ── Выбор поля ─────────────────────────────
    if step == "await_field":
        if text == "0":
            sessions.pop(user_id, None)
            await send_message(dialog_id, "✅ Сессия завершена. Напиши *редактировать* чтобы начать снова.")
            return
        if text not in FIELDS:
            await send_message(dialog_id, "⚠️ Введи цифру из списка.")
            return
        field_key, label = FIELDS[text]
        sessions[user_id]["field"] = field_key
        sessions[user_id]["field_label"] = label
        sessions[user_id]["step"] = "await_value"

        hint = ""
        if field_key == "chars":
            hint = "\n\nФормат: Цвет:Красный, Размер:XL"
        elif field_key == "category":
            hint = "\n\nВведи точное название категории WB (например: Платья)"

        await send_message(dialog_id, f"Введи новое значение для «{label}»:{hint}")
        return

    # ── Ввод нового значения ───────────────────
    if step == "await_value":
        artikuls   = session["artikuls"]
        field_key  = session["field"]
        field_label= session["field_label"]
        value      = text

        await send_message(dialog_id,
            f"⏳ Обновляю {len(artikuls)} карточек...\n"
            f"Поле: {field_label}\nЗначение: {value}"
        )

        success, failed = [], []
        for art in artikuls:
            card = await wb_get_card(art)
            if not card:
                failed.append(f"{art} (не найден)")
                continue
            card = apply_field(card, field_key, value)
            ok = await wb_update_card(card)
            (success if ok else failed).append(art)

        report = f"✅ Обновлено: {len(success)}"
        if success:
            report += f" ({', '.join(success)})"
        if failed:
            report += f"\n❌ Ошибки: {', '.join(failed)}"

        # Возвращаемся к выбору поля (те же артикулы)
        sessions[user_id]["step"] = "await_field"
        await send_message(dialog_id, report + "\n\n" + field_menu())
        return

    # ── По умолчанию ───────────────────────────
    await send_message(dialog_id,
        "Привет! 👋\nНапиши *редактировать* чтобы начать массовое редактирование карточек WB."
    )

# ─────────────────────────────────────────────
# Вебхук от Битрикс24
# ─────────────────────────────────────────────
@app.post("/bitrix/webhook")
async def bitrix_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        form = await request.form()
        data = dict(form)

    logger.info("Incoming: %s", json.dumps(data, ensure_ascii=False))

    event = data.get("event", "")
    if event not in ("ONIMBOTMESSAGEADD", "ONIMJOINCHAT"):
        return JSONResponse({"status": "ignored"})

    params   = data.get("data", {}).get("PARAMS", {})
    user_id  = str(params.get("FROM_USER_ID", ""))
    dialog_id= str(params.get("DIALOG_ID", ""))
    text     = params.get("MESSAGE", "").strip()

    if not user_id or not dialog_id:
        return JSONResponse({"status": "no user"})

    await handle_message(user_id, dialog_id, text)
    return JSONResponse({"status": "ok"})

# ─────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "WB Bot running"}
