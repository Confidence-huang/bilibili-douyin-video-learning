#!/usr/bin/env python3
"""
Explicitly export decryptable Bilibili cookies into a plaintext Netscape file.
Normal extraction should use session-scoped --cookies edge|chrome instead. This
utility refuses to run without a risk acknowledgement and never overwrites an
existing file unless the caller separately allows that action.

Usage:
    python export_cookies.py edge --output cookies.txt --acknowledge-plaintext-risk
"""
import argparse  # 明确解析风险确认、输出路径和覆盖授权。
import sys
import os
import sqlite3
import shutil
import tempfile
from pathlib import Path

from runtime_output import log  # Cookie 诊断写 stderr，避免和未来 JSON 输出混在一起。


# --- 只把浏览器已经解密的 Cookie 转成 Netscape 行 ---
def build_netscape_cookie_lines(rows):
    lines = ["# Netscape HTTP Cookie File", "# Generated for one local session", ""]
    exported_count = 0                                          # 成功口径只统计真正写入值的 Cookie。
    for host, name, value, _encrypted_value in rows:
        if not value:                                           # Chrome/Edge 的 DPAPI 密文不能当成可用值。
            continue
        domain_flag = "TRUE" if host.startswith(".") else "FALSE"
        lines.append(f"{host}\t{domain_flag}\t/\tFALSE\t9999999999\t{name}\t{value}")
        exported_count += 1
    return lines, exported_count


# --- 安全写入一份确实含有明文值的 Cookie 文件 ---
def write_netscape_cookie_file(rows, output_path, *, allow_overwrite=False):
    lines, exported_count = build_netscape_cookie_lines(rows)
    if exported_count == 0:                                     # 保留既有文件，避免空结果覆盖可用凭据。
        log("No decryptable Bilibili cookies were exported; browser values are DPAPI encrypted.")
        return False
    output = Path(output_path).expanduser().resolve()
    if output.exists() and not allow_overwrite:                  # 覆盖现有凭据需要第二个独立授权。
        log(f"Refusing to overwrite existing plaintext Cookie file: {output}")
        return False
    output.parent.mkdir(parents=True, exist_ok=True)             # 显式输出目录由用户决定。
    output.write_text("\n".join(lines), encoding="utf-8")
    try:
        os.chmod(output, 0o600)                                  # POSIX 收紧权限；Windows 仍需依赖当前用户 ACL。
    except OSError:
        pass                                                     # 权限收紧失败不改变已经写入的数据语义。
    log(f"Exported {exported_count} decryptable cookies to: {output}")
    return True


def export_edge_cookies(output_path, *, allow_overwrite=False):
    """Try to export cookies from Edge's Network/Cookies database."""
    # Edge stores cookies in two locations
    edge_profile = os.path.join(
        os.environ["LOCALAPPDATA"],
        "Microsoft", "Edge", "User Data", "Default"
    )

    # Try Network/Cookies first (version 130+)
    cookie_db = os.path.join(edge_profile, "Network", "Cookies")
    if not os.path.exists(cookie_db):
        cookie_db = os.path.join(edge_profile, "Cookies")

    if not os.path.exists(cookie_db):
        print(f"ERROR: Cookie database not found at {cookie_db}")
        print("Edge may not be installed or using a different profile.")
        return False

    # Copy to temp (bypasses file lock)
    temporary_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp = temporary_file.name                                   # 先创建唯一文件，避免 mktemp 路径竞争。
    temporary_file.close()
    try:
        shutil.copy2(cookie_db, tmp)
    except PermissionError:
        os.unlink(tmp)                                           # 复制失败时移除刚创建的空临时文件。
        print("ERROR: Edge is running and locks its cookie database.")
        print("")
        print("Options:")
        print("  1. Close Edge, then run: python export_cookies.py edge")
        print("  2. Prefer fetch_bilibili.py --cookies edge for session-scoped access")
        return False

    # Extract bilibili cookies
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT host_key, name, value, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%bilibili%'"
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"ERROR reading cookies: {e}")
        os.unlink(tmp)
        return False

    os.unlink(tmp)

    if not rows:
        print("No Bilibili cookies found. Are you logged into bilibili.com?")
        return False

    return write_netscape_cookie_file(rows, output_path, allow_overwrite=allow_overwrite)


def export_chrome_cookies(output_path, *, allow_overwrite=False):
    """Similar for Chrome."""
    chrome_profile = os.path.join(
        os.environ["LOCALAPPDATA"],
        "Google", "Chrome", "User Data", "Default"
    )
    cookie_db = os.path.join(chrome_profile, "Network", "Cookies")
    if not os.path.exists(cookie_db):
        cookie_db = os.path.join(chrome_profile, "Cookies")

    if not os.path.exists(cookie_db):
        print("Chrome cookie database not found.")
        return False

    temporary_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp = temporary_file.name                                   # Chrome 使用同一安全临时文件策略。
    temporary_file.close()
    try:
        shutil.copy2(cookie_db, tmp)
    except PermissionError:
        os.unlink(tmp)
        print("ERROR: Chrome is running. Please close Chrome first.")
        return False

    conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    cursor = conn.execute(
        "SELECT host_key, name, value, encrypted_value FROM cookies "
        "WHERE host_key LIKE '%bilibili%'"
    )
    rows = cursor.fetchall()
    conn.close()
    os.unlink(tmp)

    return write_netscape_cookie_file(rows, output_path, allow_overwrite=allow_overwrite)


# --- Require two explicit decisions before writing plaintext credentials ---
def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit Bilibili plaintext Cookie export utility.")
    parser.add_argument("browser", choices=("edge", "chrome"), help="Browser profile to inspect")
    parser.add_argument("--output", required=True, help="New Netscape Cookie file path")
    parser.add_argument(
        "--acknowledge-plaintext-risk",
        action="store_true",
        help="Confirm that the output contains sensitive plaintext credentials",
    )
    parser.add_argument("--overwrite", action="store_true", help="Also allow replacing an existing output file")
    args = parser.parse_args(argv)

    if not args.acknowledge_plaintext_risk:                      # 普通调用必须停在任何浏览器访问之前。
        print(
            "Refusing to export: add --acknowledge-plaintext-risk only for a separately authorized "
            "plaintext credential export.",
            file=sys.stderr,
        )
        return 2

    print("WARNING: the output file contains sensitive plaintext credentials.", file=sys.stderr)
    browser = args.browser
    if browser == "edge":
        success = export_edge_cookies(args.output, allow_overwrite=args.overwrite)
    else:
        success = export_chrome_cookies(args.output, allow_overwrite=args.overwrite)

    if success:
        print("\nDelete the plaintext file immediately after the separately authorized operation.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
