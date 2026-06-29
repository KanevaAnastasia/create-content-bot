import ast
from pathlib import Path


def _executable_body(node: ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _is_direct_await_self_call(stmt: ast.stmt, function_name: str) -> bool:
    if not isinstance(stmt, ast.Expr):
        return False
    value = stmt.value
    if not isinstance(value, ast.Await):
        return False
    call = value.value
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == function_name
    )


def test_handlers_state_media_has_no_trivial_async_self_recursion():
    source_path = Path("app/handlers/handlers_state_media.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        body = _executable_body(node)
        if len(body) == 1 and _is_direct_await_self_call(body[0], node.name):
            offenders.append(f"{source_path}:{node.lineno}:{node.name}")

    assert offenders == []
