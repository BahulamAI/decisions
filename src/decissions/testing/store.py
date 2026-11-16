"""In-memory evaluation sink, not production persistence."""
class MemoryStore:
    def __init__(self):
        self.records = {}
    async def get_by_hash(self, tenant_id, input_hash):
        return self.records.get((tenant_id, input_hash))
    async def save(self, record):
        return self.records.setdefault((record.tenant_id, record.input_hash), record)
    async def aclose(self):
        self.records.clear()
    def close(self):
        self.records.clear()
