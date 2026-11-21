"""The cost of a run: a table of prices per model, a cost per worker, a total per run, and a limit.

Added November 20, 2026 (TODO sections 10, 11 and 12). The prices come from the `[prices]` table of the configuration file,
in dollars per million tokens, per model: `input`, `output`, `cache_read`, `cache_write` (all four are required, so that no
kind of token is silently free). A model id may be given exactly or by a prefix: the longest key that is a prefix of the
model id wins (`model-id-5` prices `model-id-5-5`; an exact id beats any prefix).

A worker's cost is, in this order:
  1. the table's price for the model that ran (the model the runner reported, else the model the route named) times the
     tokens in the worker's `usage`: `source` `price table`;
  2. else the cost the runner itself reported in `usage.cost_usd` (Claude Code does): `source` `runner`;
  3. else `None`: the model has no price (and the runner stated no cost), or the worker has no token counts.
`None` is never counted as zero. The run's total (`run_cost`) is the sum of the known costs and says it is `partial` when
any worker's cost is `None`, naming the models without a price. A script node's worker (model `script`) has no model
session and is not part of it, except a record of money a command reported (`cost_source` `reported`). Tokens the engine cannot see (a script step that calls a model itself) are not counted.

Money a command reports itself (added November 21, 2026): a command the engine runs and captures (a `run` step of a script
node, an acceptance command, the integration check) may print lines `PANGOLOOP-SPEND: <usd> [label]`; see `parse_spend`.
The engine stores them as a record with model `script`, `cost_usd` and `cost_source` `reported`; `run_cost` counts such
records (and only such: a script record without `cost_usd` is still nothing) and gives their sum as `reported_usd`.
"""
import re

TOKEN_KINDS = ('input', 'output', 'cache_read', 'cache_write')


SPEND_LINE = re.compile(r'^PANGOLOOP-SPEND:(?:[ \t]+(\S+))?(?:[ \t]+(.*?))?[ \t]*$')
MAX_STORED = 50      # spend lines and warnings kept in one record (all valid lines are still summed)
MAX_LABEL = 200
SPEND_NUMBER = re.compile(r'^\d+(?:\.\d+)?$')


def parse_spend(output: str) -> dict:
    """The spend a command reported in its WHOLE output: {usd (sum, None when no valid line), lines: [{usd, label}],
    warnings: [text]}. Only a line that STARTS with `PANGOLOOP-SPEND:` counts (the same text mid-line is ignored). The number
    is a decimal with a dot, no sign and no currency sign; a missing, negative or otherwise bad number is ignored and
    recorded in `warnings`. Zero is valid."""
    lines, warnings = [], []
    for number, line in enumerate((output or '').split('\n'), 1):
        found = SPEND_LINE.match(line.rstrip('\r'))
        if not found:
            continue
        text, label = found.group(1), found.group(2) or ''
        if text is None:
            warnings.append(f'line {number}: ignored, no amount after PANGOLOOP-SPEND:')
        elif not SPEND_NUMBER.match(text):
            warnings.append(f'line {number}: ignored, {text!r} is not a number of dollars (a decimal with a dot, 0 or more)')
        else:
            lines.append({'usd': float(text), 'label': label[:MAX_LABEL]})
    return {'usd': round(sum(item['usd'] for item in lines), 6) if lines else None, 'lines': lines, 'warnings': warnings}


def parse_prices(table, where: str = '[prices]') -> dict[str, dict[str, float]]:
    """The validated price table from the configuration file's `[prices]`: SystemExit(message) for a mistake."""
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise SystemExit(f'{where} must be a table of models')
    prices: dict[str, dict[str, float]] = {}
    for model, row in table.items():
        if not isinstance(row, dict):
            raise SystemExit(f'{where} {model}: expected {{ input = .., output = .., cache_read = .., cache_write = .. }}, '
                             f'got {row!r}')
        unknown = sorted(set(row) - set(TOKEN_KINDS))
        if unknown:
            raise SystemExit(f'{where} {model}: unknown keys {unknown}; the keys are {list(TOKEN_KINDS)}')
        missing = [kind for kind in TOKEN_KINDS if kind not in row]
        if missing:
            raise SystemExit(f'{where} {model}: missing {missing} (dollars per million tokens; write 0 where a kind is free)')
        clean = {}
        for kind in TOKEN_KINDS:
            value = row[kind]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise SystemExit(f'{where} {model}: {kind} = {value!r} is not a number of dollars per million tokens (0 or more)')
            clean[kind] = float(value)
        prices[str(model)] = clean
    return prices


def find_price(prices: dict, *models) -> tuple[str, dict] | None:
    """(the table's key, its price) for the first of `models` that has one: an exact key, else the longest key that is a
    prefix of the model id. None when no model has a price."""
    for model in models:
        if not model:
            continue
        if model in prices:
            return model, prices[model]
        best = max((key for key in prices if model.startswith(key)), key=len, default=None)
        if best is not None:
            return best, prices[best]
    return None


def worker_cost(record: dict, prices: dict) -> tuple[float | None, str | None]:
    """(cost in dollars or None, source or None) for one worker's record; see the module's text."""
    usage = record.get('usage') or {}
    found = find_price(prices, record.get('reported_model'), record.get('model'))
    if found and usage:
        price = found[1]
        cost = sum(int(usage.get(kind) or 0) * price[kind] for kind in TOKEN_KINDS) / 1_000_000
        return round(cost, 6), 'price table'
    if isinstance(usage.get('cost_usd'), (int, float)) and not isinstance(usage.get('cost_usd'), bool):
        return round(float(usage['cost_usd']), 6), 'runner'
    return None, None


def cost_of(record: dict, prices: dict) -> float | None:
    """The cost stored in the record, else worked out now."""
    if 'cost_usd' in record:
        return record['cost_usd']
    return worker_cost(record, prices)[0]


def run_cost(records: list[dict], prices: dict, limit: float | None = None) -> dict:
    """The run's cost from its workers' records: {usd, partial, models_without_price, workers, workers_with_cost,
    workers_without_tokens, limit_usd, reported_usd}. `usd` is the sum of the known costs (None when workers ran and none has a known
    cost); `partial` is true when some worker's cost is unknown; `models_without_price` names the models of those workers
    that have no price and no cost from the runner (sorted); `workers_without_tokens` counts those whose model has a price
    but whose runner stated no tokens."""
    total, known, unknown = 0.0, 0, 0
    reported, reported_records = 0.0, 0
    unpriced: set[str] = set()
    no_tokens = 0
    counted = 0
    for record in records:
        if record.get('model') == 'script':
            if record.get('cost_source') == 'reported' and isinstance(record.get('cost_usd'), (int, float)):
                reported += record['cost_usd']   # money a command reported itself; the record is no worker
                reported_records += 1
            continue
        counted += 1
        cost = cost_of(record, prices)
        if cost is None:
            unknown += 1
            if find_price(prices, record.get('reported_model'), record.get('model')) is None:
                unpriced.add(str(record.get('reported_model') or record.get('model') or '?'))
            else:
                no_tokens += 1   # a price exists, but the runner stated no tokens
        else:
            total += cost
            known += 1
    total += reported
    return {'usd': round(total, 6) if known or reported_records or not counted else None, 'partial': bool(unknown),
            'models_without_price': sorted(unpriced), 'workers': counted, 'workers_with_cost': known,
            'workers_without_tokens': no_tokens, 'limit_usd': limit,
            'reported_usd': round(reported, 6)}
