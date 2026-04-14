import psycopg2
from psycopg2 import OperationalError

def test_opengauss_connection():
    conn = None
    try:
        # Connect using explicit parameters to safely handle the '@' in the password
        print("Connecting to openGauss database...")
        conn = psycopg2.connect(
            host="localhost",              # Replace with your actual host IP/domain
            port="15432",
            dbname="contexthub",
            user="contexthub",
            password="ContextHub@123"
        )
        print("✅ Connection successful!")

        # Create a cursor to execute SQL commands
        cur = conn.cursor()

        # --- WRITE TEST ---
        print("\nTesting Write Operation...")
        
        # 1. Create a temporary test table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS python_test_table (
                id SERIAL PRIMARY KEY,
                message VARCHAR(255)
            )
        """)
        
        # 2. Insert data and return the ID
        insert_query = "INSERT INTO python_test_table (message) VALUES (%s) RETURNING id;"
        cur.execute(insert_query, ("Hello from Python to openGauss!",))
        
        # Fetch the ID of the newly inserted row
        inserted_id = cur.fetchone()[0]
        
        # Commit the transaction to save the write
        conn.commit()
        print(f"✅ Write successful! Inserted row with ID: {inserted_id}")

        # --- READ TEST ---
        print("\nTesting Read Operation...")
        
        # Select the data we just inserted
        select_query = "SELECT id, message FROM python_test_table WHERE id = %s;"
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

