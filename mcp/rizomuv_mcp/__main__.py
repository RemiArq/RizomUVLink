"""rizomuv-mcp: the stdio MCP server, or (--link-worker) its link worker process.

Nothing may reach stdout before the SDK claims it inside run(): it is the JSON-RPC wire.
argparse's --help goes to stdout, which is acceptable only because a client never asks
for it; errors go to stderr.
"""
import sys


def main(argv=None):
    from . import config as _config
    try:
        cfg = _config.parse(argv)
    except _config.ConfigError as e:
        sys.stderr.write("rizomuv-mcp: %s\n" % e)
        return 2
    if cfg.link_worker:
        from . import worker
        return worker.main(cfg.log_level)
    from . import server
    server.build_server(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
