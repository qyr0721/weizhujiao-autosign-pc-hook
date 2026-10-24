#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sign_pc.py — PC 侧签到器 CLI（核心在 ../signcore.py）

  python signer/sign_pc.py             # 常驻
  python signer/sign_pc.py --once      # 只查一轮
  python signer/sign_pc.py --dry-run   # 只打印不提交

退出码：0 正常 / 2 号已 401（需续号）/ 3 无号 / 4 查询失败
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import signcore  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--openid-file", default=os.path.join(ROOT, "openid.txt"))
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = signcore.load_cfg(args.config)
    return signcore.sign_loop(cfg, args.openid_file,
                              once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
