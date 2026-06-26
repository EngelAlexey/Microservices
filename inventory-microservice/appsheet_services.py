"""Cliente mínimo de la API de AppSheet para el inventory-microservice.

Port en Python de `lib/appsheet/client.ts` (Kaizen-AI). Permite escribir filas vía la API
de AppSheet (acciones Add/Edit) en lugar de escribir directo a `bdBayco`, de modo que AppSheet
conoce el cambio de inmediato y la fila aparece sin que el usuario tenga que sincronizar a mano.

Solo se usa para `bcItems` y `bcItemsLns` (catálogo). El resto de tablas se sigue escribiendo
por BD. Corre como dueño del app (sin `RunAsUserEmail`) para no chocar con los filtros de
seguridad de AppSheet.

Variables de entorno (app de Bayco, las mismas de Kaizen-AI sin sufijo _KAIZEN):
- APPSHEET_APP_ID
- APPSHEET_ACCESS_KEY
- APPSHEET_REGION (por defecto "api")
"""
import os
import re
import time
import json
import logging
import datetime

import requests

logger = logging.getLogger(__name__)

APPSHEET_APP_ID = os.getenv("APPSHEET_APP_ID", "")
APPSHEET_ACCESS_KEY = os.getenv("APPSHEET_ACCESS_KEY", "")
APPSHEET_REGION = os.getenv("APPSHEET_REGION", "api")

_TIMEOUT_S = 60
_RETRY_DELAYS = [0.5, 1.5]  # solo se reintenta en errores transitorios (5xx/429/red)

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2}:\d{2})")


class AppSheetError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body or ""
        super().__init__(f"AppSheet API error {status}: {self.body[:500]}")


def is_configured() -> bool:
    """True si hay credenciales de AppSheet en el entorno."""
    return bool(APPSHEET_APP_ID and APPSHEET_ACCESS_KEY)


def _normalize_value(v):
    """Normaliza un valor para el payload de AppSheet (Locale en-US).

    - None -> "" (AppSheet no acepta null para columnas de texto)
    - bool -> "Y"/"N" (formato Sí/No de AppSheet)
    - datetime/date -> MM/DD/YYYY [HH:mm:ss]
    - strings ISO (YYYY-MM-DD[ HH:mm:ss]) -> MM/DD/YYYY (red de seguridad)
    - el resto se deja igual (números, texto)
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, datetime.datetime):
        return v.strftime("%m/%d/%Y %H:%M:%S")
    if isinstance(v, datetime.date):
        return v.strftime("%m/%d/%Y")
    if isinstance(v, str):
        m = _ISO_DATE.match(v)
        if m:
            return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
        m = _ISO_DT.match(v)
        if m:
            return f"{m.group(2)}/{m.group(3)}/{m.group(1)} {m.group(4)}"
    return v


def _normalize_row(row):
    return {k: _normalize_value(v) for k, v in row.items()}


def _is_transient(status):
    return status >= 500 or status == 429


def appsheet_mutate(table, action, rows, run_as_user_email=None):
    """Ejecuta una acción Add/Edit sobre una tabla de AppSheet.

    Devuelve la lista de filas de la respuesta (puede ir vacía). Lanza AppSheetError si la
    API responde con un error no transitorio o devuelve un campo Error. Reintenta solo ante
    errores transitorios (5xx/429/red).
    """
    if not is_configured():
        raise AppSheetError(0, "AppSheet no configurado (falta APPSHEET_APP_ID/APPSHEET_ACCESS_KEY)")
    if action not in ("Add", "Edit"):
        raise ValueError(f"Acción AppSheet no soportada: {action}")

    url = f"https://{APPSHEET_REGION}.appsheet.com/api/v2/apps/{APPSHEET_APP_ID}/tables/{table}/Action"
    properties = {"Locale": "en-US"}
    if run_as_user_email:
        properties["RunAsUserEmail"] = run_as_user_email
    payload = {
        "Action": action,
        "Properties": properties,
        "Rows": [_normalize_row(r) for r in rows],
    }
    headers = {"Content-Type": "application/json", "ApplicationAccessKey": APPSHEET_ACCESS_KEY}
    body = json.dumps(payload)

    max_attempts = len(_RETRY_DELAYS) + 1
    last_err = None
    for attempt in range(max_attempts):
        try:
            resp = requests.post(url, headers=headers, data=body, timeout=_TIMEOUT_S)
        except requests.RequestException as e:
            last_err = AppSheetError(0, f"error de red: {e}")
            if attempt < max_attempts - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise last_err

        text = resp.text or ""
        if not resp.ok:
            err = AppSheetError(resp.status_code, text)
            if _is_transient(resp.status_code) and attempt < max_attempts - 1:
                last_err = err
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise err

        if not text.strip():
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise AppSheetError(resp.status_code, f"JSON inválido: {text[:200]}")
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            if parsed.get("Error"):
                raise AppSheetError(resp.status_code, str(parsed["Error"]))
            if isinstance(parsed.get("Rows"), list):
                return parsed["Rows"]
        return []

    raise last_err or AppSheetError(0, "AppSheet inalcanzable")
