`connection_test.py` test connection and basic read/write to a opengauss database.

Unsupported Features:
* `verify_LISTEN_UNLISTEN_NOTIFY.py`: opengauss does not support LISTEN/UNLISTEN/NOTIFY.
* `vector_asyncpg.py`: opengauss is incompatible with asyncpg w.r.t. vector dtype, `message: unhandled standard data type 'vector' (OID 8305)`

demo:
* `demo_e2e_opengauss.py`: run demo with opengauss backend.
* `cleanup_demo_data.py`: cleanup memories inserted by demo script.
