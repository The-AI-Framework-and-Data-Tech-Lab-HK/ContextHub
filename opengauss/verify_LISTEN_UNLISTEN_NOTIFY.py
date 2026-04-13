import asyncio
import time
import asyncpg

DSN = "postgresql://contexthub:ContextHub%40123@localhost:15432/contexthub"
CHANNEL = f"listen_notify_test_{int(time.time())}"
PAYLOAD = "hello-from-notify"


def print_exc(prefix: str, exc: Exception) -> None:
    print(f"{prefix}: FAIL")
    print(f"  type     = {type(exc).__name__}")
    print(f"  sqlstate = {getattr(exc, 'sqlstate', None)}")
    print(f"  message  = {exc}")


async def main():
    listener_conn = None
    sender_conn = None
    got_event = asyncio.Event()
    received = []

    def on_notify(connection, pid, channel, payload):
        received.append(
            {
                "pid": pid,
                "channel": channel,
                "payload": payload,
            }
        )
        got_event.set()

    try:
        print(f"Connecting to {DSN}")
        listener_conn = await asyncpg.connect(DSN)
        sender_conn = await asyncpg.connect(DSN)
        print("CONNECT: OK\n")

        # 1) LISTEN
        print(f"[1] Testing LISTEN on channel: {CHANNEL}")
        listen_ok = False
        try:
            await listener_conn.add_listener(CHANNEL, on_notify)
            listen_ok = True
            print("LISTEN: OK")
        except Exception as e:
            print_exc("LISTEN", e)
        print()

        # 2) NOTIFY
        print(f"[2] Testing NOTIFY on channel: {CHANNEL}")
        notify_ok = False
        try:
            sql = f"NOTIFY {CHANNEL}, '{PAYLOAD}'"
            result = await sender_conn.execute(sql)
            notify_ok = True
            print(f"NOTIFY: OK ({result})")
        except Exception as e:
            print_exc("NOTIFY", e)
        print()

        # 3) Delivery
        print("[3] Testing notification delivery")
        if listen_ok and notify_ok:
            try:
                await asyncio.wait_for(got_event.wait(), timeout=2.0)
                print("DELIVERY: OK")
                print(f"  received = {received[0]}")
            except asyncio.TimeoutError:
                print("DELIVERY: FAIL")
                print("  message  = LISTEN and NOTIFY executed, but no notification was received within 2s")
        else:
            print("DELIVERY: SKIPPED")
            print("  reason   = LISTEN or NOTIFY already failed")
        print()

        # 4) UNLISTEN
        print("[4] Testing UNLISTEN *")
        try:
            result = await listener_conn.execute("UNLISTEN *")
            print(f"UNLISTEN: OK ({result})")
        except Exception as e:
            print_exc("UNLISTEN", e)
        print()

    finally:
        if sender_conn is not None:
            await sender_conn.close()
        if listener_conn is not None:
            await listener_conn.close()


if __name__ == "__main__":
    asyncio.run(main())
