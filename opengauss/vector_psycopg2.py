"""
test read/write to opengauss database with vector datatype
"""
import psycopg2
from psycopg2 import OperationalError

DEFAULT_DSN = "postgresql://contexthub:ContextHub%40123@localhost:15432/contexthub"

def test_opengauss_connection():
    conn = None
    try:
        # Connect using explicit parameters to safely handle the '@' in the password
        print("Connecting to openGauss database...")
        conn = psycopg2.connect(DEFAULT_DSN)
        print("✅ Connection successful!")

        # Create a cursor to execute SQL commands
        cur = conn.cursor()

        # --- WRITE TEST ---
        print("\nTesting Write Operation...")
        
        # 1. Create a temporary test table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS python_test_table (
                id SERIAL PRIMARY KEY,
                message VARCHAR(255),
                emb  vector(3)
            )
        """)
        
        # 2. Insert data and return the ID
        insert_query = "INSERT INTO python_test_table (message, emb) VALUES ('hello', '[1,2,3]') RETURNING id;"
        cur.execute(insert_query)
        
        # Fetch the ID of the newly inserted row
        inserted_id = cur.fetchone()[0]
        
        # Commit the transaction to save the write
        conn.commit()
        print(f"✅ Write successful! Inserted row with ID: {inserted_id}")

        # --- READ TEST ---
        print("\nTesting Read Operation...")
        
        # Select the data we just inserted
        select_query = "SELECT * FROM python_test_table WHERE id = %s;"
        cur.execute(select_query, (inserted_id,))
        
        record = cur.fetchone()
        print(f"✅ Read successful! Fetched data: {record}")

        # --- CLEANUP ---
        print("\nCleaning up test data...")
        cur.execute("DROP TABLE python_test_table;")
        conn.commit()
        print("✅ Table dropped successfully!")

    except OperationalError as e:
        print(f"❌ Connection failed: {e}")
    except Exception as e:
        print(f"❌ An error occurred during database operations: {e}")
        # Rollback the transaction on error
        if conn is not None:
            conn.rollback()
    finally:
        # Always close the cursor and connection
        if conn is not None:
            cur.close()
            conn.close()
            print("\nDatabase connection closed.")

if __name__ == "__main__":
    test_opengauss_connection()

