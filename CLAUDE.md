# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Estructura del repo

Monorepo `Microservices` del ecosistema Kaizen. Hoy contiene **un solo servicio**:

- **`inventory-microservice/`** — backend FastAPI de IA e inventario para la app de AppSheet de Bayco. **Toda la documentación de arquitectura, comandos, credenciales y reglas operativas vive en [`inventory-microservice/CLAUDE.md`](inventory-microservice/CLAUDE.md). Léelo antes de trabajar en ese servicio.**

Cada servicio es autónomo (su propio `requirements.txt`, `.env` y `CLAUDE.md`). Trabaja dentro del directorio del servicio, no desde la raíz. Al agregar un servicio nuevo, dale su propio `CLAUDE.md` y añádelo a la lista de arriba.

## Reglas que aplican a todo el repo

- La base de datos `bdBayco` (Cloud SQL/MySQL) es **producción compartida** con la app de AppSheet. AppSheet controla el esquema; no agregues ni alteres columnas desde el código. Antes de modificar datos haz `SELECT` para verificar y modifica únicamente lo autorizado (ver memorias `bdbayco-edit-rules` y `bdbayco-connection`).
- Secretos por `.env` y archivos no versionados (ver `.gitignore`): `service_account.json` y `.claude/settings.local.json` nunca se commitean.
- No hay tests ni linter configurados.
