# Load balancing model calls

OffGrid can rotate model requests across several OpenAI-compatible servers.
This is useful when two local runtimes are available, or when one server is
being restarted while another can keep working.

It is a **client-side round-robin load balancer**, not an HTTP proxy: no new
port is opened, requests still go directly from OffGrid to each configured
endpoint, and the existing offline and loopback checks apply to every endpoint.

## Command line

```console
offgrid run "port the parser to rust" \
  --endpoints http://127.0.0.1:8080/v1,http://127.0.0.1:8081/v1

offgrid chat \
  --endpoints http://127.0.0.1:8080/v1 \
  --endpoints http://127.0.0.1:11434/v1
```

`--endpoints` accepts a comma-separated list and can be repeated. It takes
precedence over the single `--endpoint` option. One endpoint works too, but
there is no balancing benefit.

The same values can be set in `.offgrid.json`:

```json
{
  "endpoints": [
    "http://127.0.0.1:8080/v1",
    "http://127.0.0.1:8081/v1"
  ]
}
```

or with `OFFGRID_ENDPOINTS`, using a comma-separated value.

## What happens on each call

1. OffGrid remembers the next endpoint and sends the chat completion there.
2. On success, the next call starts at the following endpoint.
3. If that endpoint cannot be reached or returns an error, the same request is
   tried on each other configured endpoint in order.
4. If every endpoint fails, the run raises a clear error instead of silently
   using a server that was not configured.

The position is in memory and starts at the first endpoint on each process
start. It is not persisted and is not shared between separate OffGrid runs.
This is simple round-robin: it does not measure queue depth, latency, GPU load,
or token throughput, so do not describe it as least-connections or adaptive
balancing.

## Requirements and limits

- Every endpoint must speak the OpenAI chat-completions API.
- The configured `--model` must be available on each endpoint, or each server
  must be able to choose a default. `offgrid doctor` and the connection message
  report how many endpoints answered.
- All endpoints receive the same configured API key, if one is set.
- Each endpoint follows the normal OffGrid rule: loopback by default, and a
  remote host only with `--allow-remote`. `--offline` blocks network tools; it
  does not add a new exception for load-balanced endpoints and does not change
  the existing local-model behavior.
- A request is retried on another endpoint only before a completion is
  returned. There is no streaming in this client, so there is no partially
  streamed result to merge or replay.
- The load balancer does not provide session affinity. Separate calls from one
  agent run may land on different servers.
- Endpoint failures are counted in memory and are not written to the session
  as a separate health record.
- This is separate from `--fleet`. Fleet routes calls to registered machines
  and has its own LAN discovery, shared token and fallback rules. You can use
  either feature, but they solve different problems.

## Verification

The tests use two local fake model servers and cover rotation, failover,
all-endpoints-failed errors, the remote-endpoint guard, endpoint normalization,
and selection through `open_provider`. They make no external network requests.
