import os
import shutil
import sys
import tempfile
from pathlib import Path

# 必须在任何 app.* 导入之前设定环境（app.db 在导入期固化 DATA_DIR/FIXTURES_DIR）
_TMP = Path(tempfile.mkdtemp(prefix="hourspan-reconcile-test-"))
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["FIXTURES_DIR"] = str(_TMP / "fixtures")
Path(os.environ["FIXTURES_DIR"]).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REAL_FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
for _name in ("seed_stores.json", "ingest_change_batch.json"):
    shutil.copy(_REAL_FIXTURES / _name, Path(os.environ["FIXTURES_DIR"]) / _name)

import pytest  # noqa: E402

from app import seed  # noqa: E402
from app.db import DB_PATH  # noqa: E402


@pytest.fixture()
def fresh_db():
    """每个用例一个全新库（含种子门店与种子当前镜）。"""
    if DB_PATH.exists():
        DB_PATH.unlink()
    seed.init_db()
    return DB_PATH
