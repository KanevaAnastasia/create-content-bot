"""Download and verify the local foreground segmentation model (one-time setup)."""
import hashlib
from pathlib import Path
import sys
import requests
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.title_depth import MODEL_PATH, MODEL_URL, MODEL_MD5

if MODEL_PATH.is_file():
    with MODEL_PATH.open('rb') as stream:
        if hashlib.file_digest(stream,'md5').hexdigest()==MODEL_MD5:
            print('Foreground model is ready.');sys.exit(0)
MODEL_PATH.parent.mkdir(parents=True,exist_ok=True)
pending=MODEL_PATH.with_suffix('.onnx.part')
with requests.get(MODEL_URL,stream=True,timeout=(20,120)) as response:
    response.raise_for_status()
    with pending.open('wb') as stream:
        for chunk in response.iter_content(1024*1024):stream.write(chunk)
with pending.open('rb') as stream:
    digest=hashlib.file_digest(stream,'md5').hexdigest()
if digest!=MODEL_MD5:
    pending.unlink(missing_ok=True)
    raise RuntimeError('Foreground model checksum mismatch')
pending.replace(MODEL_PATH)
print('Foreground model installed and verified.')
