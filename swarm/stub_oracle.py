import asyncio

async def stub_oracle_loop(bus, interval_s=10):
    while True:
        snap = bus.snapshot()               # {(agent_name, underlying): latest Signal}
        if snap:
            print("\n[oracle] current bus snapshot:")
            for (agent, underlying), sig in snap.items():
                print(f"   {agent:12s} {underlying:6s} {sig.direction:8s} strength={sig.strength}")
        else:
            print("[oracle] bus empty, waiting...")
        await asyncio.sleep(interval_s)