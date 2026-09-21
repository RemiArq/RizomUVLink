"""Server configuration, from the command line first and the environment second.

MCP clients start a stdio server with a fixed command line and an env block, so every
option exists in both forms; the command line wins when both are given.
"""
import argparse
import os
from dataclasses import dataclass

INSTANCE_MODES = ("auto", "attach", "headless")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class Config:
    instance: str = "auto"          # auto: the artist's open RizomUV, else a private headless one
    port: int | None = None         # attach to exactly this link port
    exe: str | None = None          # the rizomuv executable of headless launches (None: resolved)
    launch_timeout: float = 180.0   # seconds a headless launch may take to answer
    log_level: str = "INFO"
    link_worker: bool = False       # internal: this process is the link worker


class ConfigError(ValueError):
    """A bad option value; the message names the option and what it accepts."""


def _port(value, source):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError("%s must be a TCP port number, not %r" % (source, value)) from None
    if not 0 < port < 65536:
        raise ConfigError("%s must be a TCP port number (1-65535), not %d" % (source, port))
    return port


def _timeout(value, source):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ConfigError("%s must be a number of seconds, not %r" % (source, value)) from None
    if not seconds > 0:
        raise ConfigError("%s must be positive, not %s" % (source, value))
    return seconds


def _choice(value, choices, source):
    if value not in choices:
        raise ConfigError("%s must be one of %s, not %r" % (source, ", ".join(choices), value))
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rizomuv-mcp",
        description="MCP server (stdio) driving RizomUV through RizomUVLink.")
    parser.add_argument("--instance", choices=INSTANCE_MODES,
                        help="auto (default): attach to the artist's open RizomUV, else start a private "
                             "headless one; attach: only attach; headless: always start a private one "
                             "[env RIZOMUV_MCP_INSTANCE]")
    parser.add_argument("--port", help="attach to exactly this link port, an instance started with "
                                       "-id <port> [env RIZOMUV_MCP_PORT]")
    parser.add_argument("--exe", help="RizomUV executable for headless launches [env RIZOMUV_EXE]")
    parser.add_argument("--launch-timeout", help="seconds a headless launch may take to answer "
                                                 "(default 180) [env RIZOMUV_MCP_LAUNCH_TIMEOUT]")
    parser.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS,
                        help="stderr logging level (default INFO) [env RIZOMUV_MCP_LOG_LEVEL]")
    parser.add_argument("--link-worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def parse(argv=None, env=None):
    """The Config for this command line and environment. argparse exits (code 2, message on
    stderr) on an unknown option; a bad value from either source raises ConfigError."""
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)

    def pick(arg, name):
        if arg is not None:
            return arg, "--" + name.lower().replace("_", "-")
        return (env.get("RIZOMUV_MCP_" + name) or None), "RIZOMUV_MCP_" + name

    instance, src = pick(args.instance, "INSTANCE")
    instance = _choice(instance.lower(), INSTANCE_MODES, src) if instance else "auto"
    port, src = pick(args.port, "PORT")
    port = _port(port, src) if port is not None else None
    # --exe only: RIZOMUV_EXE is read where the executable is resolved (launch.find_rizomuv_exe),
    # which names the right source in its errors and in session_info
    exe = args.exe or None
    timeout, src = pick(args.launch_timeout, "LAUNCH_TIMEOUT")
    timeout = _timeout(timeout, src) if timeout is not None else 180.0
    level, src = pick(args.log_level, "LOG_LEVEL")
    level = _choice(level.upper(), LOG_LEVELS, src) if level else "INFO"
    return Config(instance=instance, port=port, exe=exe, launch_timeout=timeout, log_level=level,
                  link_worker=args.link_worker)
