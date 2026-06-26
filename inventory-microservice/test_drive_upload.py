"""Prueba aislada de subida a Google Drive.

Usa el MISMO service account y la MISMA función (upload_image_to_drive) que el
microservicio, pero NO toca la base de datos. Sube un PNG 1x1 a la carpeta dada
y reporta el file id o el error exacto.

Uso (desde inventory-microservice/, con service_account.json presente):
    python test_drive_upload.py                      # usa la unidad compartida conocida
    python test_drive_upload.py <FOLDER_ID>          # prueba otra carpeta (ej. la de "Mi unidad")
"""
import sys
import base64
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from drive_services import get_drive_service, upload_image_to_drive

# Carpeta de imágenes del inventario (unidad compartida) que usa el flujo de URL.
DEFAULT_FOLDER = "1ERWmzE8HY7HgnoT5Un0OuMSD2Mf0m11g"

# PNG 1x1 válido (rojo), para no depender de Pillow.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def main():
    folder_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FOLDER

    print(f"\n== Verificando service account ==")
    service = get_drive_service()
    if not service:
        print("ERROR: no se pudo construir el cliente de Drive (¿falta service_account.json?).")
        sys.exit(1)

    # Quién es el service account
    try:
        about = service.about().get(fields="user(emailAddress)").execute()
        print(f"Autenticado como: {about.get('user', {}).get('emailAddress')}")
    except Exception as e:
        print(f"(no se pudo leer 'about': {e})")

    print(f"\n== Subiendo PNG de prueba a folder {folder_id} ==")
    try:
        # Llamada directa SIN el try/except silencioso de upload_image_to_drive,
        # para ver el error real (insufficientParentPermissions, etc.).
        import io
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(io.BytesIO(PNG_1X1), mimetype="image/png", resumable=False)
        f = service.files().create(
            body={"name": "test_drive_upload.png", "parents": [folder_id]},
            media_body=media,
            fields="id, name, parents, driveId",
            supportsAllDrives=True,
        ).execute()
        print(f"OK -> file id: {f.get('id')}  name: {f.get('name')}  driveId: {f.get('driveId')}")
        print("\nEXITO: el service account SI puede escribir en esa carpeta.")
    except Exception as e:
        print(f"\nFALLO al subir: {type(e).__name__}: {e}")
        print("Si dice 'insufficientParentPermissions' o cuota -> esa carpeta NO sirve para el SA.")


if __name__ == "__main__":
    main()
