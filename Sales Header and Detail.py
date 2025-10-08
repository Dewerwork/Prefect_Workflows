from __future__ import annotations

import io
import json
import os
import re as _re
import pandas as pd

from prefect import flow, task, get_run_logger
from prefect.blocks.system import Secret

from google.oauth2.service_account import Credentials as SACredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# =========================
# CONFIG — customize here
# =========================
GDRIVE_ROOT_ID = "17RMLol0SgHKMcbptSswdYFJjB0MeKCQH"  # Drive folder ID
CSV_IN_HEADER  = "Dewer - Open SalesOrderHeader - 9-28-2025.csv"
CSV_IN_DETAIL  = "Dewer - OpenSalesOrderDetail - 9-28-2025.csv"
XLS_OUT_HEADER = "PFS - Open Sales Order Header Draft - 10.7.2025.xlsx"
XLS_OUT_DETAIL = "PFS - Open Sales Order Detail Draft - 10-7-2025.xlsx"

# =========================
# Google Drive helpers
# =========================
def _drive_service():
    # Load service account JSON from Prefect Secret (block name must exist)
    raw = Secret.load("gdrive-service-account").get()
    info = json.loads(raw) if isinstance(raw, str) else raw

    # Validate basic fields to avoid silent fallbacks
    for key in ("type", "client_email", "private_key", "token_uri"):
        if key not in info:
            raise ValueError(f"Service account JSON missing '{key}'")

    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = SACredentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def _find_file_id(service, folder_id: str, name: str) -> str | None:
    # Escape single quotes for the Drive query language (strings are quoted with single quotes)
    safe_name = name.replace("'", "\\'")
    q = f"'{folder_id}' in parents and name = '{safe_name}' and trashed = false"

    resp = service.files().list(
        q=q,
        fields="files(id,name)",
        pageSize=1,
        supportsAllDrives=True,         # in case the folder is on a shared drive
        includeItemsFromAllDrives=True,
    ).execute()

    files = resp.get("files", [])
    return files[0]["id"] if files else None

def read_csv_from_drive(service, folder_id: str, filename: str, **base_kwargs) -> pd.DataFrame:
    file_id = _find_file_id(service, folder_id, filename)
    if not file_id:
        raise FileNotFoundError(f"File not found in folder: {filename}")

    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    buf.seek(0)

    # Resilient decoding: cp1252 → cp1252+replace → latin-1
    attempts = [
        dict(encoding="cp1252", encoding_errors="strict"),
        dict(encoding="cp1252", encoding_errors="replace"),
        dict(encoding="latin-1", encoding_errors="strict"),
    ]
    last_err = None
    for enc in attempts:
        try:
            buf.seek(0)
            return pd.read_csv(buf, **base_kwargs, **enc)
        except UnicodeDecodeError as e:
            last_err = e
    raise last_err

def write_excel_to_drive(service, folder_id: str, filename: str, df: pd.DataFrame, sheet_name="default_1"):
    # Create xlsx in-memory, then upload/replace by name
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl", mode="w") as xw:
        df.to_excel(xw, sheet_name=sheet_name, header=True, index=False, na_rep="")
    out.seek(0)

    media = MediaIoBaseUpload(
        out,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        resumable=False,
    )

    file_id = _find_file_id(service, folder_id, filename)
    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        body = {"name": filename, "parents": [folder_id]}
        service.files().create(body=body, media_body=media, fields="id").execute()

# =================
# KNIME-ish shims
# =================
def column_filter(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in ['Ship Via Code (#1)'] if c in df.columns], errors='ignore')

def _rf_norm_name(s): return _re.sub(r'[^a-z0-9]+', '', str(s).lower())
def _rf_resolve(df: pd.DataFrame, name: str | None) -> str | None:
    if not name: return None
    if name in df.columns: return name
    lmap = {c.lower(): c for c in df.columns}
    if name.lower() in lmap: return lmap[name.lower()]
    nmap = {_rf_norm_name(c): c for c in df.columns}
    return nmap.get(_rf_norm_name(name))

def row_filter_78(df: pd.DataFrame) -> pd.DataFrame:
    return df.copy()

def row_filter_83(df: pd.DataFrame) -> pd.DataFrame:
    col_desc = _rf_resolve(df, 'Product Description')
    col_num  = _rf_resolve(df, 'Product Number')
    mask = pd.Series(True, index=df.index)
    if col_desc in df.columns: mask &= df[col_desc].notna()
    if col_num in df.columns:  mask &= df[col_num].notna()
    return df[mask].copy()

# ===================
# dtypes definitions
# ===================
DTYPES_74 = {'Customer Number': 'Int64', 'Customer Name': 'string', 'Order Number': 'string',
    'Web Suppress Price Quote': 'string', 'Use Tax': 'string', 'Truck Route Stop': 'Int64',
    'Total Tax Amount': 'Float64', 'Total Quantity To Pick': 'Int64', 'Total Quantity Open': 'Int64',
    'Total Quantity Backordered': 'Int64', 'Total Order Value': 'Float64',
    'Total Miscellaneous Charge Amount': 'Float64', 'Total Cost': 'Float64', 'Territory': 'string',
    'Terms': 'string', 'Taxable Customer': 'string', 'Tax Freight': 'string',
    'Tax Exempt Number': 'string', 'Tax Code': 'string', 'Tax Amount': 'Float64',
    'Status Code Date': 'string', 'Status Code': 'string', 'Special Inst Flat': 'string',
    'SO GP Percent': 'Float64', 'SO GP Dollars': 'Float64', 'Signed For': 'string',
    'Signature Time': 'string', 'Signature Date': 'string', 'Shipto Zip': 'string',
    'Shipto State': 'string', 'Shipto Name': 'string', 'Shipto City': 'string',
    'Shipto Addr3': 'string', 'Shipto Addr2': 'string', 'Shipto Addr1': 'string',
    'Shipping Warehouse': 'Int64', 'Shippable Weight': 'Float64', 'Shippable Volume': 'Float64',
    'Shippable Tax': 'Float64', 'Shippable HazMat Weight': 'Float64', 'Shippable Amount': 'Float64',
    'Ship W/O Deposit': 'string', 'Ship Via Code': 'string', 'Ship To Info Flat': 'string',
    'Ship To Code': 'string', 'Ship To Attention': 'string', 'Ship Date': 'string',
    'Ship Confirmed Weight': 'string', 'Ship Complete': 'string', 'Salesman Code': 'Int64',
    'Quote Under Review': 'string', 'Quote Probability %': 'Float64', 'Quote Close Date': 'string',
    'Purchasing Contact': 'string', 'Prices on Shipment Confirmation': 'string',
    'Prices on Sales Order': 'string', 'Price Contract': 'Int64', 'Original Order Number': 'string',
    'Ordered By Name': 'string', 'Ordered By ID': 'Int64', 'Order Type': 'string',
    'Order Value': 'Float64', 'Open Amount': 'Float64', 'Delivery Schedule - Current Cycle': 'string',
    'Delivery Order (Y/N)': 'string', 'Complete Order Status': 'string', 'Description': 'string',
    'Truck': 'string', 'Ship Via Code (#1)': 'string', 'Ship To ID': 'string',
    'Ship To Name': 'string', 'Ship To Address Line 1': 'string', 'Ship To Address Line 2': 'string',
    'Ship To Address Line 3': 'string', 'Ship To City': 'string', 'Ship To State': 'string',
    'Ship To Zip': 'string', 'Ship To Country': 'string'}

DTYPES_82 = {'Order Number': 'Int64', 'Product Number': 'string', 'Product Description': 'string',
    'Committed Inventory': 'string', 'Committed Quantity': 'Int64', 'Discount Percent': 'Float64',
    'Extension': 'Float64', 'External Comments Flat': 'string', 'Internal Comments Flat': 'string',
    'Line Item Gross Profit Percent': 'Float64', 'Line Item Warehouse': 'Int64',
    'Line Item Whse Avail. Qty': 'Int64', 'Line Item Whse Committed Qty': 'Int64',
    'Line Number': 'Int64', 'Net Cost': 'Float64', 'Net Price': 'Float64', 'Order Date': 'string',
    'Price Unit of Measure': 'string', 'Price Factor': 'Int64', 'Quantity Billed': 'Float64',
    'Quantity Factor': 'Int64', 'Quantity Not Shipped': 'Float64', 'Quantity Open': 'Float64',
    'Quantity Ordered': 'Int64', 'Quantity Shipped': 'Float64', 'Status Qty': 'Float64',
    'Taxable': 'string', 'Unit of Measure': 'string', 'Unit Price': 'Float64', 'Warehouses': 'Int64',
    'Main Status Abbreviated': 'string'}

def coerce_dtypes(df: pd.DataFrame, dmap: dict) -> pd.DataFrame:
    for _col, _dt in dmap.items():
        if _col not in df.columns:
            continue
        try:
            if _dt in ('Int64', 'Float64'):
                df[_col] = pd.to_numeric(df[_col], errors='coerce').astype(_dt)
            else:
                df[_col] = df[_col].astype(_dt)
        except Exception:
            pass
    return df

# ============
# TASKS
# ============
@task(retries=2, retry_delay_seconds=5)
def t_read_header(folder_id: str, file_name: str) -> pd.DataFrame:
    s = _drive_service()
    df = read_csv_from_drive(
        s, folder_id, file_name,
        sep=",", quotechar='"', header=0,
        na_values=["", " "], keep_default_na=True, skipinitialspace=True,
        low_memory=False,
    )
    df = coerce_dtypes(df, DTYPES_74)
    get_run_logger().info(f"[header] loaded {len(df):,} rows from {file_name}")
    return df

@task
def t_transform_header(df: pd.DataFrame) -> pd.DataFrame:
    return row_filter_78(column_filter(df))

@task(retries=2, retry_delay_seconds=5)
def t_write_header(folder_id: str, xls_name: str, df: pd.DataFrame):
    s = _drive_service()
    write_excel_to_drive(s, folder_id, xls_name, df)
    get_run_logger().info(f"Wrote Excel: {xls_name} (rows={len(df):,})")

@task(retries=2, retry_delay_seconds=5)
def t_read_detail(folder_id: str, file_name: str) -> pd.DataFrame:
    s = _drive_service()
    df = read_csv_from_drive(
        s, folder_id, file_name,
        sep=",", quotechar='"', header=0,
        na_values=["", " "], keep_default_na=True, skipinitialspace=True,
        low_memory=False,
    )
    df = coerce_dtypes(df, DTYPES_82)
    get_run_logger().info(f"[detail] loaded {len(df):,} rows from {file_name}")
    return df

@task
def t_transform_detail(df: pd.DataFrame) -> pd.DataFrame:
    return row_filter_83(df)

@task(retries=2, retry_delay_seconds=5)
def t_write_detail(folder_id: str, xls_name: str, df: pd.DataFrame):
    s = _drive_service()
    write_excel_to_drive(s, folder_id, xls_name, df)
    get_run_logger().info(f"Wrote Excel: {xls_name} (rows={len(df):,})")

# ============
# FLOW
# ============
@flow(name="PFS Sales (Drive → Excel)")
def pfs_sales_flow(
    gdrive_root_id: str = GDRIVE_ROOT_ID,
    csv_in_header: str = CSV_IN_HEADER,
    csv_in_detail: str = CSV_IN_DETAIL,
    xls_out_header: str = XLS_OUT_HEADER,
    xls_out_detail: str = XLS_OUT_DETAIL,
):
    logger = get_run_logger()

    # Header branch
    df_header = t_transform_header(t_read_header(gdrive_root_id, csv_in_header))
    t_write_header(gdrive_root_id, xls_out_header, df_header)

    # Detail branch
    df_detail = t_transform_detail(t_read_detail(gdrive_root_id, csv_in_detail))
    t_write_detail(gdrive_root_id, xls_out_detail, df_detail)

    logger.info("Flow completed ✅")

if __name__ == "__main__":
    pfs_sales_flow()
