"""命令行入口。

    python -m wxbot run [--config config/config.yaml]
    python -m wxbot activate <激活码>
"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wxbot")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="启动机器人")
    run.add_argument("--config", default="config/config.yaml")
    act = sub.add_parser("activate", help="激活 wxautox4")
    act.add_argument("code")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "activate":
        from wxbot.drivers.wxautox4_driver import activate

        ok = activate(args.code)
        print("激活成功" if ok else "激活失败，激活码无效或已过期")
        sys.exit(0 if ok else 1)

    from wxbot.config import load_settings
    from wxbot.core.bot import Bot
    from wxbot.drivers.wxautox4_driver import WxAutoX4Driver

    settings = load_settings(getattr(args, "config", "config/config.yaml"))
    driver = WxAutoX4Driver(groups=set(settings.listen.groups))
    Bot(settings, driver).run_forever()


if __name__ == "__main__":
    main()
