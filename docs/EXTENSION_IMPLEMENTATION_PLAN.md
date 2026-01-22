# Zelos Protocol Extension Implementation Plan

A reusable template based on the Modbus extension implementation. Use this to build OPC-UA, BACnet, or other protocol extensions.

---

## Phase 1: Project Setup

### 1.1 Scaffold Structure
```
zelos-extension-{protocol}/
├── zelos_extension_{protocol}/
│   ├── __init__.py
│   ├── client.py          # Main client with Zelos SDK integration
│   ├── {protocol}_map.py  # Protocol-specific address/node definitions
│   ├── cli/
│   │   └── app.py         # CLI and app mode entry points
│   └── demo/
│       ├── simulator.py   # Demo server for testing
│       └── demo_config.json
├── tests/
│   └── test_{protocol}.py
├── main.py
├── pyproject.toml
├── Justfile
├── CLAUDE.md
└── README.md
```

### 1.2 Core Dependencies
```toml
dependencies = [
    "zelos-sdk>=0.0.8",
    "{protocol-library}",  # e.g., opcua-asyncio, pymodbus
    "rich-click>=1.8.0",
]
```

### 1.3 Dev Tooling
- **Linter**: ruff (strict mode)
- **Pre-commit**: ruff-check + ruff-format
- **Testing**: pytest with real server integration tests
- **Task runner**: just (Justfile)

---

## Phase 2: Data Model

### 2.1 Define Protocol-Specific Types
Create a dataclass for addressable items (Modbus: Register, OPC-UA: Node):

```python
@dataclass
class Register:  # or Node for OPC-UA
    address: int          # or node_id: str for OPC-UA
    name: str
    type: str             # register type or node class
    datatype: str         # uint16, float32, bool, string, etc.
    unit: str = ""
    scale: float = 1.0
    byte_order: str = "big"  # protocol-specific options
    writable: bool = None    # auto-detect from type if None
```

### 2.2 User-Defined Events
Group protocol items into semantic events (not protocol-native groupings):

```json
{
  "name": "device_name",
  "events": {
    "temperature": [
      {"name": "pcb", "address": 100, "datatype": "float32", "unit": "°C"},
      {"name": "ambient", "address": 102, "datatype": "float32", "unit": "°C"}
    ],
    "setpoints": [
      {"name": "high_limit", "address": 200, "datatype": "uint16", "writable": true}
    ]
  }
}
```

### 2.3 Data Type Coverage
Support at minimum:
| Type | Size | Notes |
|------|------|-------|
| bool | 1 bit | For digital I/O |
| uint16/int16 | 16-bit | Common register size |
| uint32/int32 | 32-bit | Counters, accumulators |
| float32 | 32-bit | Analog values |
| uint64/int64 | 64-bit | Large counters |
| float64 | 64-bit | High precision |
| string | variable | Device names, IDs |

---

## Phase 3: Client Implementation

### 3.1 Client Class Structure
```python
class ProtocolClient:
    def __init__(self, ..., register_map: RegisterMap | None = None):
        # Connection params
        # Protocol-specific client instance
        # Zelos trace source
        self._running = False
        self._connected = False
        self._poll_count = 0
        self._error_count = 0

    async def connect(self) -> bool: ...
    async def disconnect(self) -> None: ...
    async def read_value(self, item) -> Any: ...
    async def write_value(self, item, value) -> bool: ...

    def start(self) -> None: ...  # Init trace source
    def stop(self) -> None: ...
    def run(self) -> None: ...    # Blocking poll loop
```

### 3.2 Zelos SDK Integration
```python
def _init_trace_source(self):
    self._source = zelos_sdk.TraceSourceCacheLast(source_name)

    for event_name, items in self.register_map.events.items():
        fields = [
            zelos_sdk.TraceEventFieldMetadata(
                item.name,
                self._get_sdk_datatype(item.datatype),
                item.unit
            )
            for item in items
        ]
        self._source.add_event(event_name, fields)

def _get_sdk_datatype(self, datatype: str) -> zelos_sdk.DataType:
    mapping = {
        "bool": zelos_sdk.DataType.Boolean,  # Note: Boolean not Bool
        "uint16": zelos_sdk.DataType.UInt16,
        "int16": zelos_sdk.DataType.Int16,
        # ... etc
    }
    return mapping.get(datatype, zelos_sdk.DataType.Int32)
```

### 3.3 Reconnection Logic
```python
async def _run_async(self):
    reconnect_interval = 3.0

    while self._running:
        if not await self._ensure_connected():
            logger.warning(f"Connection failed, retrying in {reconnect_interval}s...")
            await asyncio.sleep(reconnect_interval)
            continue

        try:
            values = await self._poll()
            await self._log_values(values)
        except Exception as e:
            if self._is_connection_error(e):
                self._connected = False
                continue  # Reconnect immediately

        await asyncio.sleep(self.poll_interval)

def _is_connection_error(self, error: Exception) -> bool:
    indicators = ["connection", "timeout", "refused", "reset", "broken pipe"]
    return any(ind in str(error).lower() for ind in indicators)
```

---

## Phase 4: SDK Actions

### 4.1 Required Actions
| Action | Description |
|--------|-------------|
| Get Status | Connection state, poll count, error count |
| Read by Address | Raw protocol read |
| Write by Address | Raw protocol write |
| Read Named | Read by item name from map |
| Write Named | Write by item name (with writability check) |
| List Items | All mapped items |
| List Writable | Only writable items |

### 4.2 Action Pattern
```python
@zelos_sdk.action("Write Named", "Write value to named item")
@zelos_sdk.action.text("name", title="Item Name")
@zelos_sdk.action.number("value", title="Value")
def write_named(self, name: str, value: float) -> dict[str, Any]:
    if not self.register_map:
        return {"error": "No map loaded", "success": False}

    item = self.register_map.get_by_name(name)
    if not item:
        return {"error": f"'{name}' not found", "success": False}

    if not item.writable:
        return {"error": f"'{name}' is not writable", "success": False}

    success = asyncio.run(self._write_value(item, value))
    return {"name": name, "value": value, "success": success}
```

---

## Phase 5: Demo Server

### 5.1 Purpose
- Test without hardware
- Exercise all data types
- Exercise all register/node types
- Test byte order variants
- Provide realistic changing values

### 5.2 Demo Coverage Checklist
- [ ] All item types (holding, input, coil, etc. / variable, property, method)
- [ ] All data types (uint16 through float64, bool, string)
- [ ] All byte orders (big, little, swapped variants)
- [ ] Read-only items
- [ ] Writable items
- [ ] Items with scale factors
- [ ] Dynamic values that change over time

### 5.3 Simulator Pattern
```python
class DeviceSimulator:
    def update(self, dt: float) -> dict[str, Any]:
        """Generate realistic values. Called periodically."""
        return {
            "voltage": 230 + random.gauss(0, 2),
            "current": 50 + random.gauss(0, 5),
            "energy": self.energy_total,  # Accumulating
            "relay1": self.relay_state,   # Stateful
        }
```

---

## Phase 6: Testing Strategy

### 6.1 Test Categories
1. **Unit Tests** (no network)
   - Data type encoding/decoding
   - Map parsing
   - Action logic without connection
   - Error detection logic

2. **Integration Tests** (with demo server)
   - Read all item types
   - Write all writable types
   - Byte order handling
   - Write rejection for read-only items
   - Event polling

### 6.2 Test Fixture Pattern
```python
@pytest.fixture(scope="module")
def demo_server():
    server = DemoServer(port=15020)
    server.start()
    yield server
    server.stop()

@pytest.fixture
def client(demo_server, register_map):
    client = ProtocolClient(host=demo_server.host, port=demo_server.port, ...)
    asyncio.get_event_loop().run_until_complete(client.connect())
    yield client
    asyncio.get_event_loop().run_until_complete(client.disconnect())
```

### 6.3 Test Count Target
Aim for 60-80+ tests covering:
- 15-20 unit tests for data model
- 10-15 unit tests for encoding/decoding
- 5-10 unit tests for simulator logic
- 15-20 integration tests for read/write
- 10-15 unit tests for actions
- 3-5 tests for reconnection logic

---

## Phase 7: Documentation

### 7.1 CLAUDE.md (for AI assistance)
```markdown
## Build & Development
just install / check / format / test / dev

## Code Style
- Linter: ruff (strict)
- Use contextlib.suppress() instead of try-except-pass
- Imports sorted by ruff

## Key Files
- client.py, {protocol}_map.py, simulator.py, test_{protocol}.py
```

### 7.2 README.md Structure
1. One-line description
2. Features (bullet list)
3. Quick Start (3-4 commands)
4. Configuration (table)
5. Item Map format (JSON example + field table)
6. Data Types (compact table)
7. Actions (table)
8. Development (just commands)
9. Links
10. License

---

## OPC-UA Specific Considerations

### Key Differences from Modbus
| Aspect | Modbus | OPC-UA |
|--------|--------|--------|
| Addressing | Numeric (0-65535) | NodeId (string/numeric) |
| Data model | Flat registers | Hierarchical nodes |
| Discovery | None | Browse address space |
| Security | None built-in | Certificates, encryption |
| Data types | Limited | Rich (including arrays, structs) |
| Subscriptions | Poll only | Native pub/sub |

### OPC-UA Specific Features to Consider
1. **Certificate handling** - Security mode configuration
2. **Node browsing** - Auto-discover available nodes
3. **Subscriptions** - Use monitored items instead of polling
4. **Namespaces** - Handle namespace indices in node IDs
5. **Method calls** - Support OPC-UA method invocation
6. **Complex types** - Arrays, structures, extension objects

### Suggested OPC-UA Map Format
```json
{
  "name": "plc_device",
  "events": {
    "temperature": [
      {"name": "sensor1", "node_id": "ns=2;s=Temperature.Sensor1", "datatype": "float32"},
      {"name": "sensor2", "node_id": "ns=2;i=1001", "datatype": "float32"}
    ],
    "status": [
      {"name": "running", "node_id": "ns=2;s=Status.Running", "datatype": "bool"}
    ]
  }
}
```

---

## Implementation Checklist

### Week 1: Foundation
- [ ] Project scaffold
- [ ] Data model (Node class, NodeMap)
- [ ] Basic client (connect, disconnect, read)
- [ ] Unit tests for data model

### Week 2: Core Features
- [ ] Write operations
- [ ] All data types
- [ ] Zelos SDK integration (trace source, events)
- [ ] Integration tests

### Week 3: Polish
- [ ] Demo server with simulator
- [ ] SDK Actions (all 7+)
- [ ] Reconnection logic
- [ ] Action tests

### Week 4: Production Ready
- [ ] Security configuration (OPC-UA specific)
- [ ] Documentation
- [ ] Edge case handling
- [ ] Final test coverage review
