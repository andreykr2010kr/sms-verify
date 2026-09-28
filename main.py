"""
Сервер верификации для сайта chisto-tochka-spb.ru
Использует звонки-авторизацию (Code Call) через sms.ru
Клиенту звонит робот, клиент вводит последние 4 цифры номера
"""

import time
import random
import re
from collections import defaultdict
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Хранилище кодов: {phone: {"code": "1234", "expires": timestamp}}
codes = {}

# Лимиты
ip_requests = defaultdict(list)
phone_requests = defaultdict(list)

MAX_CALLS_PER_IP = 3       # максимум 3 звонка с одного IP в час
MAX_CALLS_PER_PHONE = 1    # максимум 1 звонок на номер в час
CODE_EXPIRE = 300          # код живёт 5 минут (звонок короткий)


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
    if phone[1] != "9":
        return False
    return True


def check_ip_limit(ip: str) -> bool:
    now = time.time()
    ip_requests[ip] = [t for t in ip_requests[ip] if now - t < 3600]
    if len(ip_requests[ip]) >= MAX_CALLS_PER_IP:
        return False
    ip_requests[ip].append(now)
    return True


def check_phone_limit(phone: str) -> bool:
    now = time.time()
    phone_requests[phone] = [t for t in phone_requests[phone] if now - t < 3600]
    if len(phone_requests[phone]) >= MAX_CALLS_PER_PHONE:
        return False
    phone_requests[phone].append(now)
    return True


@app.post("/send-code")
async def send_code(req: SendCodeRequest, request: Request):
    """Отправка звонка-авторизации через sms.ru"""
    # Проверка капчи
    if not req.captcha or not req.captcha_answer:
        return {"success": False, "error": "Пройдите проверку"}

    try:
        user_answer = int(req.captcha)
        correct_answer = int(req.captcha_answer)
        if user_answer != correct_answer:
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
        return {"success": False, "error": "Звонок уже отправлен. Проверьте входящий вызов."}

    # Отправляем звонок через sms.ru Code Call
    import os
    api_id = os.environ.get("SMSRU_API_ID", "")
    if not api_id:
        return {"success": False, "error": "Сервер не настроен"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://sms.ru/code/call",
            params={
                "api_id": api_id,
                "phone": phone,
                "ip": client_ip,
                "json": 1,
            },
        )
        data = resp.json()

    if data.get("status") == "OK":
        # sms.ru возвращает код в поле code
        call_code = str(data.get("code", ""))
        if not call_code:
            return {"success": False, "error": "Ошибка при отправке звонка"}

        codes[phone] = {"code": call_code, "expires": time.time() + CODE_EXPIRE}
        return {"success": True, "message": "Звонок отправлен на номер " + phone}
    else:
        error_msg = data.get("status_text", "Не удалось позвонить. Проверьте номер.")
        return {"success": False, "error": error_msg}


@app.post("/verify-code")
async def verify_code(req: VerifyCodeRequest):
    """Проверка кода из звонка"""
    phone = normalize_phone(req.phone)
    if not phone:
        return {"success": False, "error": "Неверный номер"}

    stored = codes.get(phone)
    if not stored:
        return {"success": False, "error": "Запросите звонок повторно"}

    if time.time() > stored["expires"]:
        del codes[phone]
        return {"success": False, "error": "Код истёк. Запросите новый звонок."}

    if req.code.strip() == stored["code"]:
        del codes[phone]
        return {"success": True, "message": "Номер подтверждён"}
    else:
        return {"success": False, "error": "Неверный код"}


@app.get("/")
async def health():
    return {"status": "ok"}
