import os
from pathlib import Path


def load_env_value(key: str) -> str | None:
    value = os.getenv(key)
    if value:
        return value
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def load_env_values_multi(key: str) -> list[str]:
    raw = load_env_value(key)
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def mask_key(key: str) -> str:
    tail = key[-4:] if len(key) >= 4 else key
    return f"***{tail}"


def main() -> None:
    keys: list[str] = []
    single = load_env_value("GEMINI_API_KEY")
    if single:
        keys.append(single)
    for k in load_env_values_multi("GEMINI_API_KEYS"):
        if k not in keys:
            keys.append(k)
    if not keys:
        raise RuntimeError("GEMINI_API_KEY / GEMINI_API_KEYS not found in env or .env")
    try:
        from google import genai  # type: ignore
    except Exception as e:
        raise RuntimeError("Missing dependency. Install with: pip install google-genai") from e

    merged: dict[str, set[str]] = {}
    for idx, api_key in enumerate(keys, start=1):
        print(f"\n=== KEY {idx}/{len(keys)} {mask_key(api_key)} ===")
        client = genai.Client(api_key=api_key)
        try:
            for m in client.models.list():
                methods = getattr(m, "supported_actions", None) or getattr(m, "supported_methods", None) or []
                methods_list = [str(x) for x in methods]
                print(f"Name: {m.name}, Supported Methods: {methods_list}")
                merged.setdefault(str(m.name), set()).update(methods_list)
        except Exception as e:
            print(f"Failed for key {mask_key(api_key)}: {e}")

    print("\n=== MERGED UNIQUE MODELS ===")
    for name in sorted(merged.keys()):
        print(f"Name: {name}, Supported Methods: {sorted(merged[name])}")


if __name__ == "__main__":
    main()
