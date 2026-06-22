#!/usr/bin/env bash
set -o errexit

# Instalar librerías
pip install -r requirements.txt

# Preparar archivos estáticos
python manage.py collectstatic --no-input

# Migrar la base de datos (se ejecutará sobre Postgres en Render)
python manage.py migrate