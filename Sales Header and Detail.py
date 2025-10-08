from pathlib import Path
import pandas as pd
import re as _re
from gdrive_fsspec import GoogleDriveFileSystem

from prefect import flow, task, get_run_logger
from prefect.tasks import task_input_hash
from datetime import timedelta
import tempfile, os

# =========================
# CONFIG — customize here
# =========================

GDRIVE_ROOT_ID = "17RMLol0SgHKMcbptSswdYFJjB0MeKCQH"  # your folder ID
CSV_IN_HEADER  = "Dewer - Open SalesOrderHeader - 9-28-2025.csv"
CSV_IN_DETAIL  = "Dewer - OpenSalesOrderDetail - 9-28-2025.csv"
XLS_OUT_HEADER = "PFS - Open Sales Order Header Draft - 10.7.2025.xlsx"
XLS_OUT_DETAIL = "PFS - Open Sales Order Detail Draft - 10-7-2025.xlsx"

# ==============
# Drive helpers
# ==============
def make_fs() -> GoogleDriveFileSystem:
    # First run may prompt OAuth if token cache is empty
    return GoogleDriveFileSystem(token="cache", root_file_id=GDRIVE_ROOT_ID)

def gdrive_list(fs: GoogleDriveFileSystem):
    try:
        print("Drive folder contents:", fs.ls(""))
    except Exception as e:
        print("Could not list Drive folder:", e)

def read_gdrive_csv(fs: GoogleDriveFileSystem, filename: str, **read_csv_kwargs) -> pd.DataFrame:
    with fs.open(filename, "rb") as f:
        return pd.read_csv(f, **read_csv_kwargs)

def write_gdrive_excel_atomic(fs, filename: str, df: pd.DataFrame,
                              sheet_name="default_1", mode="w"):
    """
    Write Excel locally, then upload to Drive atomically.
    This avoids SSL EOFs during long streaming writes.
    """
    # 1) Write to a local temp file
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with pd.ExcelWriter(tmp_path, engine="openpyxl", mode="w") as xw:
            df.to_excel(xw, sheet_name=sheet_name, header=True, index=False, na_rep="")

        # 2) Upload in one shot
        try:
            fs.put(tmp_path, filename)        # modern fsspec
        except AttributeError:
            fs.put_file(tmp_path, filename)   # older fsspec fallback

    finally:
        # 3) Clean up local temp
        try:
            os.remove(tmp_path)
        except OSError:
            pass




# --- Resilient CSV reader that survives odd bytes like 0x9D ---
def read_gdrive_csv_resilient(fs, filename: str, **base_kwargs) -> pd.DataFrame:
    """
    Try cp1252 first; if it fails due to undefined bytes (e.g., 0x9D),
    retry with cp1252 + replacement, then latin-1 as a last resort.
    """
    # Never stream in "chunksize" here; we want a single pass for reliability.
    attempts = [
        dict(encoding="cp1252", encoding_errors="strict"),
        dict(encoding="cp1252", encoding_errors="replace"),  # inserts � for bad bytes
        dict(encoding="latin-1", encoding_errors="strict"),  # decodes all bytes 0x00..0xFF
    ]

    last_err = None
    for i, enc_kw in enumerate(attempts, 1):
        try:
            with fs.open(filename, "rb") as f:
                return pd.read_csv(f, **base_kwargs, **enc_kw)
        except UnicodeDecodeError as e:
            last_err = e
            # Try next encoding strategy
    # If we somehow got here, re-raise the last error
    raise last_err



# =================
# KNIME-ish shims
# =================
def column_filter(df: pd.DataFrame) -> pd.DataFrame:
    exclude_cols = ['Ship Via Code (#1)']
    return df.drop(columns=[c for c in exclude_cols if c in df.columns], errors='ignore')

def _rf_norm_name(s): return _re.sub(r'[^a-z0-9]+', '', str(s).lower())

def _rf_resolve(df: pd.DataFrame, name: str | None) -> str | None:
    if name is None:
        return None
    if name in df.columns:
        return name
    lmap = {c.lower(): c for c in df.columns}
    if name.lower() in lmap:
        return lmap[name.lower()]
    nmap = {_rf_norm_name(c): c for c in df.columns}
    return nmap.get(_rf_norm_name(name))

def row_filter_78(df: pd.DataFrame) -> pd.DataFrame:
    # KNIME node 78 – Your “Complete Order Status” rule was neutral; keep rows unchanged.
    return df.copy()

def row_filter_83(df: pd.DataFrame) -> pd.DataFrame:
    # Keep rows where Product Description and Product Number are both present
    col_desc = _rf_resolve(df, 'Product Description')
    col_num  = _rf_resolve(df, 'Product Number')
    mask = pd.Series(True, index=df.index)
    if col_desc in df.columns:
        mask &= df[col_desc].notna()
    if col_num in df.columns:
        mask &= df[col_num].notna()
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
def t_read_header(fs, csv_name: str) -> pd.DataFrame:
    logger = get_run_logger()
    df = read_gdrive_csv_resilient(
        fs, csv_name,
        sep=",", quotechar='"', header=0,
        # keep your parsing options:
        na_values=["", " "], keep_default_na=True, skipinitialspace=True,
        low_memory=False,
        # let the resilient reader decide encoding
    )
    df = coerce_dtypes(df, DTYPES_74)
    logger.info(f"[header] loaded {len(df):,} rows from {csv_name}")
    return df

@task
def t_transform_header(df: pd.DataFrame) -> pd.DataFrame:
    df1 = column_filter(df)
    df2 = row_filter_78(df1)
    return df2

@task(retries=2, retry_delay_seconds=5)
def t_write_header(fs, df: pd.DataFrame, xls_name: str):
    write_gdrive_excel_atomic(fs, xls_name, df, sheet_name="default_1", mode="w")
    get_run_logger().info(f"Wrote Excel: {xls_name} (rows={len(df):,})")

@task(retries=2, retry_delay_seconds=5)
def t_read_detail(fs, csv_name: str) -> pd.DataFrame:
    logger = get_run_logger()
    df = read_gdrive_csv_resilient(
        fs, csv_name,
        sep=",", quotechar='"', header=0,
        na_values=["", " "], keep_default_na=True, skipinitialspace=True,
        low_memory=False,
    )
    df = coerce_dtypes(df, DTYPES_82)
    logger.info(f"[detail] loaded {len(df):,} rows from {csv_name}")
    return df

@task
def t_transform_detail(df: pd.DataFrame) -> pd.DataFrame:
    # Equivalent to: (ref sorter passthrough) -> row filter 83 -> (date passthrough)
    return row_filter_83(df)


@task(retries=2, retry_delay_seconds=5)
def t_write_detail(fs, df: pd.DataFrame, xls_name: str):
    write_gdrive_excel_atomic(fs, xls_name, df, sheet_name="default_1", mode="w")
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
    fs = GoogleDriveFileSystem(token="cache", root_file_id=gdrive_root_id)

    # (Optional) show folder contents once
    try:
        logger.info(f"Drive folder listing: {fs.ls('')}")
    except Exception as e:
        logger.warning(f"Could not list folder: {e}")

    # Header branch
    df_header_raw = t_read_header(fs, csv_in_header)
    df_header     = t_transform_header(df_header_raw)
    t_write_header(fs, df_header, xls_out_header)

    # Detail branch
    df_detail_raw = t_read_detail(fs, csv_in_detail)
    df_detail     = t_transform_detail(df_detail_raw)
    t_write_detail(fs, df_detail, xls_out_detail)

    logger.info("Flow completed ✅")

if __name__ == "__main__":
    # Ad-hoc local run
    pfs_sales_flow()
