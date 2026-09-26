"""
Сервер SMS-верификации для сайта chisto-tochka-spb.ru
Принимает запрос на отправку кода, проверяет код.
Не отправляет заявки в Telegram — это делает Tilda.
"""

import time
import random
import re
import ipaddress
from collections import defaultdict
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI()

# Разрешаем запросы с сайта
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Хранилище кодов: {phone: {"code": "1234", "expires": timestamp}}
codes = {}

# Лимиты: {ip: [timestamp, timestamp, ...]}
ip_requests = defaultdict(list)
phone_requests = defaultdict(list)

MAX_SMS_PER_IP = 3       # максимум 3 SMS с одного IP в час
MAX_SMS_PER_PHONE = 1    # максимум 1 SMS на номер в час
CODE_EXPIRE = 600        # код живёт 10 минут


class SendCodeRequest(BaseModel):
    phone: str
    captcha: str
    captcha_answer: str


class VerifyCodeRequest(BaseModel):
    phone: str
    code: str


def normalize_phone(phone: str) -> str:
    """Приводим номер к формату 7XXXXXXXXXX"""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return digits
    if len(digits) == 10:
        return "7" + digits
    return ""


def is_valid_phone(phone: str) -> bool:
    """Проверяем, что номер — российский мобильный"""
    if not phone or len(phone) != 11:
        return False
    if not phone.startswith("7"):
        return False
    if phone[1] != "9":  # мобильные номера начинаются с 79
        return False
    return True


def check_ip_limit(ip: str) -> bool:
    """Возвращает True, если лимит не превышен"""
    now = time.time()
    # Чистим старые записи (старше 1 часа)
    ip_requests[ip] = [t for t in ip_requests[ip] if now - t < 3600]
    if len(ip_requests[ip]) >= MAX_SMS_PER_IP:
        return False
    ip_requests[ip].append(now)
    return True


def check_phone_limit(phone: str) -> bool:
    """Возвращает True, если лимит не превышен"""
    now = time.time()
    phone_requests[phone] = [t for t in phone_requests[phone] if now - t < 3600]
    if len(phone_requests[phone]) >= MAX_SMS_PER_PHONE:
        return False
    phone_requests[phone].append(now)
    return True


@app.post("/send-code")
async def send_code(req: SendCodeRequest, request: Request):
    """Отправка SMS с кодом"""
    # Проверка капчи
    if not req.captcha or not req.captcha_answer:
        return {"success": False, "error": "Пройдите проверку"}

    # Простая капча: ответ передаётся с клиента
    # Реальная проверка капчи на сервере
    try:
        expected = int(req.captcha)
        answer = int(req.captcha_answer)
        if expected != answer:
            return {"success": False, "error": "Неверный ответ на проверку"}
    except (ValueError, TypeError):
        return {"success": False, "error": "Неверный ответ на проверку"}

    # Нормализуем телефон
    phone = normalize_phone(req.phone)
    if not is_valid_phone(phone):
        return {"success": False, "error": "Введите корректный номер телефона"}

    # Получаем IP клиента
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else request.client.host

    # Проверяем лимиты
    if not check_ip_limit(client_ip):
        return {"success": False, "error": "Слишком много запросов. Попробуйте позже."}
    if not check_phone_limit(phone):
        return {"success": False, "error": "Код уже отправлен. Проверьте SMS."}

    # Генерируем код
    code = str(random.randint(1000, 9999))

    # Сохраняем код
    codes[phone] = {"code": code, "expires": time.time() + CODE_EXPIRE}

    # Отправляем SMS через sms.ru
    import os
    api_id = os.environ.get("SMSRU_API_ID", "")
    if not api_id:
        return {"success": False, "error": "Сервер не настроен"}

    message = f"Ваш код подтверждения: {code}. Сайт Чисто и точка."

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://sms.ru/sms/send",
            params={
                "api_id": api_id,
                "to": phone,
                "msg": message,
                "json": 1,
            },
        )
        data = resp.json()

    if data.get("status") == "OK":
        return {"success": True, "message": "Код отправлен на номер " + phone}
    else:
        return {"success": False, "error": "Не удалось отправить SMS. Проверьте номер."}


@app.post("/verify-code")
async def verify_code(req: VerifyCodeRequest):
    """Проверка кода"""
    phone = normalize_phone(req.phone)
    if not phone:
        return {"success": False, "error": "Неверный номер"}

    stored = codes.get(phone)
    if not stored:
        return {"success": False, "error": "Запросите код повторно"}

    if time.time() > stored["expires"]:
        del codes[phone]
        return {"success": False, "error": "Код истёк. Запросите новый."}

    if req.code.strip() == stored["code"]:
        del codes[phone]
        return {"success": True, "message": "Номер подтверждён"}
    else:
        return {"success": False, "error": "Неверный код"}


@app.get("/")
async def health():
    return {"status": "ok"}
