# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es esto

Microservicio FastAPI (`inventory-microservice`) que actúa como backend de IA y motor de inventario para una app de AppSheet. AppSheet llama a los endpoints `/webhook/*` y `/admin/*`; el servicio digitaliza facturas (PDF) con Gemini, scrapea/resuelve productos, escribe en la base de datos MySQL compartida (`bdBayco` en Cloud SQL) y dispara difusiones por WhatsApp vía BuilderBot. La mayoría de las escrituras van directo a la BD; **excepción: el catálogo (`bcItems`/`bcItemsLns`) se escribe vía la API de AppSheet** (`appsheet_services.py`) para que el artículo aparezca sin sync manual, con fallback a BD si AppSheet falla.

No hay tests ni linter configurados en el repo.

## Comandos

```powershell
# Instalar dependencias
pip install -r requirements.txt

# Ejecutar en desarrollo (recarga automática, puerto 10000 por defecto)
python main.py
# o
uvicorn main:app --reload --host 0.0.0.0 --port 10000
```

La configuración vive en `.env` (no versionado). Variables usadas en el código:
`DB_USER`, `DB_PASS`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `GEMINI_API_KEY`, `GOOGLE_API_KEY` (fallback de descarga de Drive para archivos públicos), `BUILDERBOT_URL` + `BUILDERBOT_API_KEY` (WhatsApp), `DEFAULT_IMAGE_FOLDER_ID`, `PORT`, `DEV_RELOAD` (opcional: activa la recarga automática de uvicorn al correr `python main.py`), `APPSHEET_APP_ID` + `APPSHEET_ACCESS_KEY` + `APPSHEET_REGION` (API de AppSheet de la app de Bayco; ver `appsheet_services.py`).

## Credenciales y secretos por ruta

El código busca secretos primero en rutas de producción (Render: `/etc/secrets/...`) y cae a rutas locales:
- **Service account de Google Drive**: `/etc/secrets/service_account[.json]` → `service_account.json` (raíz del microservicio). Sin él, la descarga de Drive cae al fallback con `GOOGLE_API_KEY`.
- **Certificado SSL de Cloud SQL**: `/etc/secrets/server-ca.pem` → `certs/server-ca.pem`. La conexión usa SSL pero con `check_hostname=False` y `verify_mode=CERT_NONE` (`database.py`).

## Arquitectura

Flujo en capas, separando IO externo de la lógica de negocio:

- **`main.py`** — Define la app FastAPI, los modelos Pydantic de payload y los endpoints. El trabajo bloqueante (Drive, scraping, IA, chequeo de duplicados) se delega a un `ThreadPoolExecutor` vía `run_in_executor` para no bloquear el event loop. Las difusiones de WhatsApp corren como `BackgroundTask`.
- **`ai_services.py`** — Cliente de Gemini (`gemini-2.5-flash`). Cada función envuelve un prompt distinto: facturas CR (`extract_invoice_data`), datos de empresa (`extract_company_data`), producto desde HTML (`extract_product_from_html`), producto desde código de barras con Google Search grounding (`extract_product_from_barcode`). Importante: el código de barras usa la herramienta `google_search`, que es **incompatible** con `response_mime_type=application/json`, por eso parsea el JSON manualmente (`_parse_json_object`).
- **`scrape_services.py`** — Descarga páginas de producto con `requests` y reduce el HTML a JSON-LD + texto del body antes de mandarlo a la IA (`extract_relevant_content`). Nota: no funciona con sitios SPA que renderizan por JS (ver memoria de El Lagar).
- **`drive_services.py`** — Resuelve IDs/paths/URLs de Google Drive, descarga PDFs y sube imágenes (con permiso público `anyone:reader`). Reintentos con backoff exponencial.
- **`appsheet_services.py`** — Cliente mínimo de la API de AppSheet (port de `lib/appsheet/client.ts` de Kaizen-AI). `appsheet_mutate(table, action, rows, user_settings=...)` ejecuta acciones `Add`/`Edit`, normaliza fechas a `MM/DD/YYYY` (Locale en-US), booleanos a `Y`/`N`, y reintenta ante errores transitorios. Se usa **solo para `bcItems` y `bcItemsLns`** (ver la subsección de abajo). El microservicio genera los IDs (`ItemID`/`ItemLnID`) y los pasa en el `Add`. **Imprescindible** pasar `user_settings={"DatabaseID": database_id}`: los filtros de seguridad de la app de Bayco se basan en `UserSettings("DatabaseID")`, así que sin él la API no "ve" ninguna fila (`Edit` → 404 "row not found", Refs como `UnitID` → 400 "invalid value").
- **`logic.py`** — **Toda la lógica de negocio y escritura a base de datos.** Es el archivo más grande y crítico.
- **`models.py`** — Modelos SQLAlchemy. **No usa migraciones**: las tablas ya existen en `bdBayco` (las administra AppSheet). Los modelos deben coincidir con el esquema real, no al revés.
- **`database.py`** — Engine SQLAlchemy (MySQL/pymysql) con pool y SSL. `get_db()` es la dependencia FastAPI; `SessionLocal()` se usa directo en hilos/tareas de fondo.

### Convenciones de la base de datos (clave para entender los modelos)

Las tablas usan prefijos por dominio y cada fila lleva `DatabaseID` para **multi-tenancy** (toda consulta debe filtrar por `DatabaseID`, y suele normalizarse con `[:10]`):
- `bc*` — catálogo: `bcItems` (producto genérico padre), `bcItemsLns` (variante/presentación hija), `bcBrands`, `bcRFQLns`/`bcRequestsLns` (RFQ).
- `fn*` — documentos financieros: `fnDocuments` (factura), `fnDocumentsLns` (líneas).
- `ic*` — inventario: `icMovements` (movimientos), `icItemsPrices` (buckets de valuación), `icItemsStock` (stock neto por proyecto).
- `dr*` — directorio: `drCompanies`, `drProjects`.

Las columnas de auditoría (`*CreatedBy`, `*ModifiedBy`) se escriben con el literal `"AI_BOT"`. Las marcas de tiempo usan `get_now_ca()` (UTC-6, naive).

### Escritura del catálogo vía AppSheet y la limitación de sync (estado actual)

El flujo `extract-from-barcode` crea/actualiza `bcItems` + `bcItemsLns` vía la API de AppSheet (con fallback a BD). Esto se hizo para que el artículo apareciera en la app **sin sync manual**. Conclusión tras probarlo en producción:

- **La escritura vía API funciona** (con `UserSettings.DatabaseID`, ver arriba), pero **no basta** para que el artículo se vea sin sincronizar. AppSheet **no empuja** los cambios al dispositivo: el cliente solo refresca al hacer sync.
- La razón de fondo es de **timing**: la resolución con Gemini (`google_search` grounding) tarda **~100 s**, y el flujo corre asíncrono (`BackgroundTask`, responde `{"status":"queued"}` al instante) porque AppSheet/Apps Script **cancela** una espera síncrona tan larga. El write del microservicio aterriza ~100 s después, fuera de la ventana del sync que dispara el escaneo, así que solo aparece en el **siguiente** sync.
- Se probó del lado de AppSheet **sin éxito** para tiempo real: `Automatic updates` solo trae cambios externos **cada ~30 min**; `Quick sync` no aplica si la app usa columnas virtuales/fórmulas externas.

**Decisión (2026-06): se deja así** — el artículo se crea correctamente y aparece tras sincronizar (no en tiempo real). Se aceptó esa limitación por ahora.

**Plan futuro (no implementado)**: que **AppSheet** sea quien inserte la fila, usando el **"Return value"** del webhook. El microservicio expondría un endpoint **síncrono** que solo resuelve y **devuelve** los datos del producto (sin escribir a BD/AppSheet), y el bot de AppSheet arma la fila con ese retorno → al originarse en AppSheet, aparece sin sync (como pasó con el step de "observación"). **Bloqueante a confirmar antes de invertir**: si el step de "Return a value" tolera esperar ~100 s sin cortar por timeout (medirlo con un endpoint que duerma N segundos); si no, habría que acelerar la extracción (p. ej. consultar una base de barcodes y dejar Gemini como fallback).

**Pendiente conocido**: el mismo barcode crea ítems duplicados (la dedup solo busca por `item_id`/`itTitle` exacto, no por `lnBarcode`). Fix propuesto: deduplicar por `lnBarcode` + `DatabaseID` antes de crear.

### Jerarquía de producto (importante para extracción)

`bcItems` (padre) = producto genérico, nombre corto sin marca/medidas (`itTitle`). `bcItemsLns` (hijo) = variante específica con todos los detalles en `itModel`/`lnSpecs`. Los prompts de IA están escritos para respetar esta separación; al modificarlos, mantenerla.

### Inventario y valuación

- `_perform_inventory_update` es la **única fuente de verdad** para mover stock: actualiza `icItemsStock` (stock neto por recinto), `icItemsPrices` (buckets de valuación) y la cantidad global en `bcItemsLns`. Todo cambio de inventario debe pasar por aquí.
- `apply_valuation_bucket_logic` y `_determine_action` replican deliberadamente la lógica de AppSheet (acciones IN/OUT/Transfer/Reserved, TOP-1 FILTER LIST). Al tocar esto, el objetivo es la paridad con AppSheet, no "mejorarlo".
- `process_single_movement_logic` es **idempotente**: bloquea la fila con `with_for_update()` y omite movimientos ya `POSTED`. Corre síncrono en el hilo HTTP a propósito (para bloquear a AppSheet hasta terminar).
- `backfill_movement_costs_logic` (endpoint `/admin/backfill-costs`) es de un solo uso por `database_id` para sanear datos históricos de costos.

### Resolución de productos en facturas

`find_product_id` mapea descripciones de línea de factura a variantes del catálogo en cascada: nombre exacto → hint exacto → substring → similitud Jaccard. Umbrales: ≥0.85 acepta, ≥0.50 marca como "posible" (`UNKNOWN` con candidato en observaciones), <0.50 no encontrado.

## Reglas operativas sobre bdBayco

Es una base de datos de **producción compartida** con la app de AppSheet. Antes de modificar datos o esquema, hacer `SELECT` para verificar, y modificar únicamente lo autorizado (ver memoria `bdbayco-edit-rules`). No agregar/alterar columnas desde aquí: el esquema lo controla AppSheet.
