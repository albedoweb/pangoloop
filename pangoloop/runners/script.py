"""The `script` stream: the events a script node writes (one per step), read by the viewer for such a node whichever
runner the run uses.

A script node has no model and no process of its own; the engine writes its events.jsonl itself, in the form the
Hermes runner reads (`tool_use`, `tool_result`, a `system`/`init` event with the model `script`, a `result`). This
runner is that form under its own name, so that the page reads a script node's folder the same way in a run of any
runner. It is not a runner to run a model with: it has no command.
"""
import dataclasses

from pangoloop.runners.hermes import HermesRunner


@dataclasses.dataclass
class ScriptStream(HermesRunner):
    name = 'script'

    def command(self, *args, **kwargs):
        raise NotImplementedError('the script stream starts no process: a script node is done by the engine')


RUNNER = ScriptStream()
