# routine

`viam-labs:service:routine` is a [generic service](https://docs.viam.com/operate/reference/services/generic/) that runs one or more state machines, called modes. Each step calls a method on another resource already configured on the machine. The routine reads the response, polls with `until`, passes variables into the next mode, and can clean up with `on_cancel`.

This routine extends a switch, waits five seconds, retracts it, and stops:

```json
{
  "start_mode": "pulse",
  "modes": {
    "pulse": {
      "steps": [
        {
          "resource": "tray-actuator-extend",
          "method": "SetPosition",
          "request": { "position": 1 }
        },
        { "sleep": "5s" },
        {
          "resource": "tray-actuator-extend",
          "method": "SetPosition",
          "request": { "position": 0 }
        }
      ]
    }
  }
}
```

`start_mode` names the mode to run. Steps run in order, and a mode with no `then` stops after its last step. A full coffee-delivery config is in [examples/coffee_delivery.json](examples/coffee_delivery.json).

## Configure

| Field | Default | Meaning |
| --- | --- | --- |
| `modes` | required | Named modes. Each has `steps`, and may have `then`, `pass`, `on_cancel`, and `on_dependency_error`. |
| `start_mode` | `idle` | Mode entered on start. Must name a configured mode. |
| `auto_start` | `true` | Start that mode when the service configures. |
| `on_dependency_error` | `retry_step` | What to do when a resource call fails. `retry_step`, `restart_mode`, `start_mode`, or `stop`. |
| `retry` | `1s`, max `30s` | Backoff for `retry_step`. Durations look like `500ms`, `10s`, `3m`, `1h`. |
| `heartbeat` | off | Status report that runs on its own timer. |

### Modes

A mode is an ordered list of steps. It may also have `then`, `pass`, `on_cancel`, and `on_dependency_error`.

Steps run one after another. The next step starts when the current one returns. A call with `"wait": false` is the exception: it runs in the background until the mode changes.

A returned call means the RPC finished. Some methods, such as navigation `navigate_to_location` and docking `Start`, return as soon as they accept the goal. Follow them with an `until` step when the routine should wait until the action itself is done.

A step is a call, an `until`, or a `sleep`. `sleep` only waits. It cannot set `then`, `pass`, or `on_dependency_error`.

### then

`then` names the mode to enter next.

On a step, `then` runs as soon as that step finishes and skips every step after it. On a mode, `then` runs only after every step has finished without its own `then`.

A mode with no `then` stops the routine. It does not repeat. The same is true of `on_cancel` when that block has no `then`.

`then` may be a fixed mode name or a single `${...}` expression evaluated after the step returns. Bare words in that expression are mode names:

```json
"then": "${if result.available then wait_for_handover else idle}"
```

### pass

`pass` is the variable set handed to the mode named by `then`. It replaces that mode's variables. Values from the current mode are not kept unless `pass` copies them.

The step's `pass` is used when that step's `then` runs. The mode's `pass` is used when the mode's `then` runs. If `then` is set and `pass` is omitted, the next mode starts with no variables.

`on_cancel`'s own `then` also starts the next mode with no variables. A step inside `on_cancel` can still pass values when that step has its own `then`.

```json
"pass": { "order": "${order}" }
```

Any string that contains `${...}` is filled in from the current variables, the latest `result`, or `status`. A string that is only `${...}` keeps the value as-is, including objects. Any other text builds a string, and each placeholder is stringified.

```json
{
  "resource": "tts",
  "method": "DoCommand",
  "request": {
    "text": "Coffee for ${result.for}"
  }
}
```

Inside `${...}` you can use paths, `split(value, sep)`, indexes, and `if … then … else …`:

```json
"text": "Coffee for ${if split(result.for, '_')[1] then split(result.for, '_')[1] else split(result.for, '_')[0]}"
```

That uses the part after `_` when present, otherwise the whole value. A missing index is false.

`{"literal": "Coffee for ${result.for}"}` keeps that text as written, with no substitution.

In the delivery example, pickup's `pass` copies the existing `order` variable. The last pickup step moves the tray, so `result` at the end of the mode is that move, not the order.

### on_cancel

`on_cancel` is a property of the mode, next to `steps`. It runs when:

- the routine receives the `cancel` command
- an `until` step's `cancel_when` becomes true
- an `until` step reaches its `timeout`

`stop` does not run `on_cancel`.

```json
"on_cancel": {
  "steps": [
    {
      "resource": "navigation",
      "method": "DoCommand",
      "request": { "command": "cancel" }
    },
    {
      "resource": "docking",
      "method": "Stop",
      "request": {}
    }
  ],
  "then": "idle"
}
```

Without `then`, the routine stops after those steps. With no `on_cancel` block at all, `cancel`, `cancel_when`, and an `until` timeout stop the routine immediately. `last_error` is `cancelled`, `cancel condition met`, or `until timed out`.

### on_dependency_error

`on_dependency_error` chooses what happens when a resource call fails, including a call step's `timeout`. An `until` timeout and `cancel_when` are cancellations, so they run `on_cancel` instead.

| Value | Effect |
| --- | --- |
| `retry_step` | Retry that call with exponential backoff. An `until` step keeps polling. Default. |
| `restart_mode` | Enter the current mode again from its first step, with the same variables. |
| `start_mode` | Enter `start_mode` with no variables. |
| `stop` | Stop the routine and set `state` to `error`. `last_error` is the failure. |

Set it on the routine, on a mode, or on a call or `until` step. The step wins, then the mode, then the routine. A step inside `on_cancel` uses that same order against the mode it belongs to.

`retry` sets the backoff for `retry_step`: `initial` defaults to `1s` and `max` defaults to `30s`. Each retry waits twice as long, up to `max`.

### Call step

```json
{
  "resource": "tray",
  "method": "Move",
  "request": { "angle_deg": 90 },
  "wait": false,
  "timeout": "5s",
  "then": "next_mode",
  "pass": { "order": "${order}" },
  "on_dependency_error": "stop"
}
```

`method` is the resource's gRPC method. `Move` and `do_command` both work. The routine fills in the resource `name`. Calls wait for a response unless `wait` is `false`, in which case the call runs in the background until the mode changes.

For `DoCommand`, `request` is the command map, the same map the Go and Python SDKs pass to `DoCommand`. The routine places it in the `command` field and sets `name` to the resource. A request whose `command` is already an object is sent as the gRPC message, so a sibling `name` there selects the resource instead.

```json
{ "resource": "navigation", "method": "DoCommand", "request": { "command": "navigate_to_location", "name": "dock_test" } }
```

`DoCommand` responses are unwrapped, so a returned `available` field is read as `result.available`.

### Sleep step

```json
{ "sleep": "5s" }
```

The routine waits after the previous step returns, then runs the next step. Stopping or cancelling the mode ends the wait.

### Until step

```json
{
  "until": {
    "every": "5s",
    "resource": "coffee_maker:orders",
    "method": "DoCommand",
    "request": { "order_status": { "order_id": "${order.id}" } },
    "when": "result.status == handed_over",
    "cancel_when": "result.status == canceled",
    "timeout": "3m"
  },
  "then": "delivering",
  "pass": { "order": "${result.order}" }
}
```

The method is polled immediately, then every `every`, until `when` is true. `cancel_when` or `timeout` runs the mode's `on_cancel` block. For an until step, `result` is the last poll.

### Expressions

`when` and `cancel_when` accept comparisons (`== != < <= > >=`), `and`, `or`, `not`, parentheses, numbers, quoted strings, `true`, `false`, and `null`. A bare word that is not a variable is a string, so `result.status == handed_over` matches the string `handed_over`. Dotted fields and indexes (`result.items[0].name`) are supported.

### Heartbeat

```json
"heartbeat": {
  "every_secs": 5,
  "resource": "fleet_status",
  "method": "DoCommand",
  "request": { "mode": "${status.mode}", "state": "${status.state}" }
}
```

`every_secs` defaults to 5. The heartbeat keeps reporting after the routine stops. A failed report is logged and does not change the mode.

## Custom APIs

A step can call a module API that is not built into the Viam SDK, such as [action-api](https://github.com/viam-labs/action-api). Use the proto method name. The routine fills in `name`, and it loads the proto from the module that provides the API.

```json
{ "resource": "brewer", "method": "Start", "request": {} }
```

```json
{
  "until": {
    "every": "1s",
    "resource": "brewer",
    "method": "IsRunning",
    "request": {},
    "when": "result.running == false"
  }
}
```

`Status` returns a map, so later steps read `result.status.<key>`.

## Commands

`do_command` accepts:

| `command` | Extra fields | Effect |
| --- | --- | --- |
| `status` | | The status object nested under `status`. |
| `GetStatus` | | That same object at the top level. `get_status` is the same command. |
| `start` | `mode`, `vars` | Start `mode`, or `start_mode`. |
| `stop` | | Stop without running `on_cancel`. |
| `set_mode` | `mode`, `vars` | Jump to a mode. |
| `cancel` | | Run the current mode's `on_cancel`. |

The service method `GetStatus` returns that object directly. That is what the machine control tab calls. A heartbeat reads the same object as `status`. Its fields are `state`, `mode`, `step_index`, `step_count`, `step_kind`, `vars`, `last_error`, `result`, `mode_entered_at`, and `updated_at`. `result` is the latest response, including each `until` poll.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Tests host fake servo, generic, and custom navigation resources on an in-process gRPC server. Custom API protos are served by a sibling module socket, the same place viam-server leaves them.
