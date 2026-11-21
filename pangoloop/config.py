"""The configuration file `pangoloop.toml`: the runner, the routes, the runner's own settings and the numeric limits.

Written in step 3 (November 18, 2026). Where it is read from: `--config <file>` (the command line sets
PANGOLOOP_CONFIG), else PANGOLOOP_CONFIG, else the `pangoloop.toml` of the project (pangoloop/project.py), else the one in the current folder, else no file. Every value has one
of three sources, and the first that has it wins: an environment variable, the file, the built-in default; `load()`
records which one, and `python3 -m pangoloop config` prints them. Without a file and without the variables everything is
what it was before step 3. A mistake in the file (an unknown table or key, a bad route, a number that is not one) stops
the command with a message and exit status 2, before anything runs.

    [runner]
    name = "claude-code"        # or "hermes"; PANGOLOOP_RUNNER wins

    [routes]                    # category = "model@provider" ("model" alone for claude-code); `default` for the rest
    default = "model@provider"
    review = "other-model@provider"

    [readers]                   # the readers of `preflight`: a name you choose = "model@provider"
    quick = "model@provider"

    [runner.claude_code]        # binary, allow, deny, default_deny, flags, persist_sessions, strict_mcp
    [runner.hermes]             # profile

    [paths]                     # runs, plans, roles: folders, relative to the project (the folder of this file)
    [view]                      # port: the page's port (else the first free one from 5420)

    [limits]                    # max_parallel, max_fix_rounds, node_limit_seconds, max_turns, acceptance_seconds, ...,
                                # max_cost_usd (a run stops starting workers once its known cost is over it)

    [prices]                    # dollars per million tokens, per model id or prefix of one (pangoloop/costs.py)
    "model-id" = { input = 3, output = 15, cache_read = 0.3, cache_write = 3.75 }
"""
import os
import re
import tomllib

from pangoloop import costs
from pangoloop.plan import DEFAULT_MAX_FIX_ROUNDS, DEFAULT_ROUTE, ROUTES

FILE_NAME = 'pangoloop.toml'


def lookup(name: str, environ: dict) -> tuple[str, str | None]:
    """(value, the variable it came from) for `name`; ('', None) when it is unset or empty."""
    value = environ.get(name, '')
    return (value, name) if value != '' else ('', None)
RUNNERS = ('hermes', 'claude-code')

# Built-in routes of the Claude Code runner: the same placeholders as the Hermes runner's (pangoloop/plan.py), with the
# provider `anthropic`. No model name is built in; the configuration file names them.
CLAUDE_ROUTES = {category: (model, 'anthropic') for category, (model, _) in ROUTES.items()}
CLAUDE_DEFAULT_ROUTE = (DEFAULT_ROUTE[0], 'anthropic')

# limit name -> (environment variable, default, type)
LIMITS = {
    'max_parallel': ('SG_MAX_PARALLEL', 3, int),
    'max_fix_rounds': ('SG_MAX_FIX_ROUNDS', DEFAULT_MAX_FIX_ROUNDS, int),
    'node_limit_seconds': ('SG_NODE_LIMIT_SECONDS', 3600, int),
    'max_turns': ('SG_MAX_TURNS', 200, int),
    'acceptance_seconds': ('SG_ACCEPTANCE_SECONDS', 1800, int),
    'integration_check_seconds': ('SG_INTEGRATION_CHECK_SECONDS', 900, int),
    'human_wait_seconds': ('SG_HUMAN_WAIT_SECONDS', 86400, int),
    'coordinator_wait_seconds': ('PANGOLOOP_COORDINATOR_WAIT_SECONDS', 900, float),
    'rulings_chars': ('PANGOLOOP_RULINGS_CHARS', 12000, int),   # size up to which every ruling goes into a prompt in full
    'max_cost_usd': ('PANGOLOOP_MAX_COST_USD', None, float),    # dollars; None: no limit (see pangoloop/costs.py)
}
TOP_LEVEL = ('runner', 'routes', 'readers', 'limits', 'prices', 'paths', 'view')
PATH_KEYS = ('runs', 'plans', 'roles')


def parse_route(category: str, value, runner: str, table: str = 'routes') -> tuple[str, str]:
    """`model@provider` -> (model, provider). For claude-code the provider may be left out or must be `anthropic`."""
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f'[{table}] {category}: expected "model@provider", got {value!r}')
    model, at, provider = value.strip().rpartition('@')
    if not at:
        model, provider = provider, ''
    if not model:
        raise SystemExit(f'[{table}] {category}: {value!r} has no model')
    if runner == 'claude-code':
        if provider not in ('', 'anthropic'):
            raise SystemExit(f'[{table}] {category}: the claude-code runner takes models of the provider "anthropic" '
                             f'only, not {provider!r}; write "{model}" or "{model}@anthropic"')
        provider = 'anthropic'
    elif not provider:
        raise SystemExit(f'[{table}] {category}: the hermes runner needs "model@provider", got {value!r}')
    return model, provider


def parse_readers(table, runner: str, where: str) -> dict[str, tuple[str, str, str | None]]:
    """`[readers]`: a reader's name -> "model@provider", or { model = "model@provider", runner = "claude-code" }.
    Returns name -> (model, provider, runner or None); None means the configured runner."""
    if not isinstance(table, dict):
        raise SystemExit(f'{where}: [readers] must be a table')
    found = {}
    for name, value in table.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
            raise SystemExit(f"[readers] {name!r}: a reader's name is letters, digits, dots, dashes and underscores")
        own = None
        if isinstance(value, dict):
            extra = sorted(set(value) - {'model', 'runner'})
            if extra:
                raise SystemExit(f'[readers] {name}: unknown keys {extra}; known: model, runner')
            own = value.get('runner')
            if own is not None:
                own = str(own).replace('_', '-')
                if own not in RUNNERS:
                    raise SystemExit(f"[readers] {name}: runner {own!r}: the runners are {', '.join(RUNNERS)}")
            value = value.get('model')
        model, provider = parse_route(name, value, own or runner, 'readers')
        found[name] = (model, provider, own)
    return found


class Config:
    """The effective configuration. `sources` maps a dotted name (`runner.name`, `routes.review`, `limits.max_turns`,
    `runner.claude_code.deny`) to where its value came from."""

    def __init__(self) -> None:
        self.path: str | None = None
        self.runner = 'hermes'
        self.routes: dict[str, tuple[str, str]] = {}
        self.default_route: tuple[str, str] = DEFAULT_ROUTE
        self.limits: dict[str, int | float | None] = {}
        self.readers: dict[str, tuple[str, str, str | None]] = {}   # name -> (model, provider, runner or None)
        self.prices: dict[str, dict[str, float]] = {}
        self.paths: dict[str, str] = {}          # `[paths]`: runs, plans, roles, as written
        self.view_port: int | None = None        # `[view] port`
        self.runner_settings: dict[str, dict] = {'hermes': {}, 'claude_code': {}}
        self.sources: dict[str, str] = {}

    def routes_table(self) -> tuple[dict, tuple]:
        """(ROUTES, DEFAULT_ROUTE) for the engine; the very objects of pangoloop.plan when nothing changed them."""
        if self.runner == 'hermes' and not any(key.startswith('routes.') for key in self.sources
                                               if self.sources[key] != 'default'):
            return ROUTES, DEFAULT_ROUTE
        return self.routes, self.default_route

    def settings(self) -> dict:
        """The settings table of the chosen runner (what `get_runner(name, settings)` takes)."""
        return self.runner_settings[self.runner.replace('-', '_')]

    def describe(self) -> str:
        lines = [f"config file: {self.path or 'none (no ' + FILE_NAME + ' in the project or the current folder, no --config)'}"]
        def row(name: str, value) -> None:
            lines.append(f'{name} = {value!r}'.ljust(58) + f"  [{self.sources.get(name, 'default')}]")
        row('runner.name', self.runner)
        for category in sorted(self.routes):
            row(f'routes.{category}', '@'.join(self.routes[category]))
        row('routes.default', '@'.join(self.default_route))
        for name, (model, provider, own) in self.readers.items():
            row(f'readers.{name}', f'{model}@{provider}' + (f' ({own})' if own else ''))
        for name in LIMITS:
            row(f'limits.{name}', self.limits[name])
        for model in sorted(self.prices):
            row(f'prices.{model}', self.prices[model])
        for table in ('hermes', 'claude_code'):
            for key, value in sorted(self.runner_settings[table].items()):
                row(f'runner.{table}.{key}', value)
        for key, value in sorted(self.paths.items()):
            row(f'paths.{key}', value)
        if self.view_port is not None:
            row('view.port', self.view_port)
        return '\n'.join(lines)


def find_file(environ: dict, cwd: str) -> tuple[str | None, str]:
    """(path or None, where the choice came from)."""
    value, variable = lookup('PANGOLOOP_CONFIG', environ)
    explicit = value.strip()
    if explicit:
        if explicit == 'none':
            return None, f'{variable}=none'
        path = os.path.abspath(explicit)
        if not os.path.isfile(path):
            raise SystemExit(f'pangoloop: no such configuration file: {explicit}')
        return path, variable
    root = environ.get('PANGOLOOP_PROJECT', '').strip()   # the project the command line chose (pangoloop/project.py)
    if root:
        path = os.path.join(root, FILE_NAME)
        if os.path.isfile(path):
            return path, 'project'
    path = os.path.join(cwd, FILE_NAME)
    if os.path.isfile(path):
        return path, 'current folder'
    return None, 'none'


def load(environ: dict | None = None, cwd: str | None = None) -> Config:
    environ = os.environ if environ is None else environ
    config = Config()
    path, _ = find_file(environ, cwd or os.getcwd())
    data: dict = {}
    if path:
        try:
            with open(path, 'rb') as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise SystemExit(f'pangoloop: cannot read {path}: {error}') from None
        config.path = path
    unknown = sorted(set(data) - set(TOP_LEVEL))
    if unknown:
        raise SystemExit(f'{path}: unknown tables {unknown}; the tables are {list(TOP_LEVEL)}')
    where = f'file {path}'
    runner_table = data.get('runner', {})
    if not isinstance(runner_table, dict):
        raise SystemExit(f'{path}: [runner] must be a table')
    known = {'name', 'hermes', 'claude_code'}
    if sorted(set(runner_table) - known):
        raise SystemExit(f'{path}: unknown keys in [runner]: {sorted(set(runner_table) - known)}; known: {sorted(known)}')

    # the runner
    config.runner, config.sources['runner.name'] = 'hermes', 'default'
    if 'name' in runner_table:
        config.runner, config.sources['runner.name'] = runner_table['name'], where
    value, variable = lookup('PANGOLOOP_RUNNER', environ)
    if value:
        config.runner, config.sources['runner.name'] = value, f'env {variable}'
    config.runner = str(config.runner).replace('_', '-')
    if config.runner not in RUNNERS:
        raise SystemExit(f"runner {config.runner!r} (from {config.sources['runner.name']}): the runners are "
                         f"{', '.join(RUNNERS)}")

    # the routes
    base, default = (ROUTES, DEFAULT_ROUTE) if config.runner == 'hermes' else (CLAUDE_ROUTES, CLAUDE_DEFAULT_ROUTE)
    config.routes, config.default_route = dict(base), default
    for category, value in (data.get('routes') or {}).items():
        route = parse_route(category, value, config.runner)
        if category == 'default':
            config.default_route = route
        else:
            config.routes[category] = route
        config.sources[f'routes.{category}'] = where
    for category, route in config.routes.items():
        config.sources.setdefault(f'routes.{category}', 'default')
    config.sources.setdefault('routes.default', 'default')

    # the readers of `preflight`: a name -> model@provider (and perhaps a runner); no name is built in
    config.readers = parse_readers(data.get('readers') or {}, config.runner, path or '')
    for name in config.readers:
        config.sources[f'readers.{name}'] = where

    # the limits
    table = data.get('limits') or {}
    if sorted(set(table) - set(LIMITS)):
        raise SystemExit(f'{path}: unknown keys in [limits]: {sorted(set(table) - set(LIMITS))}; known: {sorted(LIMITS)}')
    for name, (variable, default_value, kind) in LIMITS.items():
        value, source = default_value, 'default'
        if name in table:
            value, source = table[name], where
        given, used = lookup(variable, environ)
        if given != '':
            value, source = given, f'env {used}'
        try:
            if isinstance(value, bool):
                raise ValueError
            value = None if value is None else kind(value)
        except (TypeError, ValueError):
            raise SystemExit(f'limits.{name} (from {source}): {value!r} is not a number') from None
        if name == 'max_cost_usd' and value is not None and value < 0:
            raise SystemExit(f'limits.{name} (from {source}): {value!r} is negative')
        config.limits[name] = value
        config.sources[f'limits.{name}'] = source

    # the prices
    config.prices = costs.parse_prices(data.get('prices'), f'{path}: [prices]' if path else '[prices]')
    for model in config.prices:
        config.sources[f'prices.{model}'] = where

    # the project's folders and the page's port
    table = data.get('paths') or {}
    if not isinstance(table, dict) or sorted(set(table) - set(PATH_KEYS)):
        raise SystemExit(f'{path}: [paths] must be a table with the keys {list(PATH_KEYS)}')
    for key, value in table.items():
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f'{path}: paths.{key} must be a folder name, got {value!r}')
        config.paths[key] = value
        config.sources[f'paths.{key}'] = where
    table = data.get('view') or {}
    if not isinstance(table, dict) or sorted(set(table) - {'port'}):
        raise SystemExit(f'{path}: [view] must be a table with the key port')
    if 'port' in table:
        port = table['port']
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise SystemExit(f'{path}: view.port must be a port number (1 to 65535), got {port!r}')
        config.view_port = port
        config.sources['view.port'] = where

    # the runners' own tables
    for table_name in ('hermes', 'claude_code'):
        settings = runner_table.get(table_name, {})
        if not isinstance(settings, dict):
            raise SystemExit(f'{path}: [runner.{table_name}] must be a table')
        config.runner_settings[table_name] = dict(settings)
        for key in settings:
            config.sources[f'runner.{table_name}.{key}'] = where
    command, variable = lookup('PANGOLOOP_CLAUDE_COMMAND', environ)
    if command and config.runner == 'claude-code':
        config.runner_settings['claude_code']['binary'] = command
        config.sources['runner.claude_code.binary'] = f'env {variable}'
    return config
