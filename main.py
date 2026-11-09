import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import psycopg
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

STATUS_OPTIONS = ["已收到面单", "已打印", "已打包", "已交USPS/UPS"]
SHIPPED_STATUS = "已交USPS/UPS"
INBOUND_STATUSES = ["预报中", "已入库"]
CARRIERS = ["USPS", "UPS"]
