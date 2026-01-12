def rate_limiter(func):
    def wrapper(*args, **kwargs):
        print("Checking rate limits...")
        func(*args, **kwargs)
        print("Request processed")
    return wrapper

@rate_limiter
def process_payment(txn):
    print(f"Processing {txn}...")

class DBSession:
    def __enter__(self):
        print("Connecting to Database...")
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        print("Closing Connection.")

def main():
    with DBSession():
        print("...Executing complex transaction SQL...")
        raise ValueError

main()