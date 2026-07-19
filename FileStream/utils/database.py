import time
import logging
import asyncio
import motor.motor_asyncio
from bson.objectid import ObjectId
from bson.errors import InvalidId
from FileStream.config import Telegram
from FileStream.server.exceptions import FIleNotFound

logger = logging.getLogger(__name__)

SEP = ":"  # separator between shard index and ObjectId in a compound file id


class _Shard:
    """One MongoDB cluster acting as a single storage unit ('DB 1', 'DB 2', ...)."""

    def __init__(self, index: int, uri: str, db_name: str):
        self.index = index
        self.uri = uri
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[db_name]
        self.file = self.db.file
        self.col = self.db.users        # only used on the primary shard (index 0)
        self.black = self.db.blacklist  # only used on the primary shard (index 0)
        self._cached_used_mb = 0.0
        self._last_checked = 0.0

    async def storage_used_mb(self, force: bool = False) -> float:
        """Live storage usage in MB, cached for 60s so we don't spam dbStats."""
        now = time.time()
        if force or (now - self._last_checked) > 60:
            try:
                stats = await self.db.command("dbStats")
                self._cached_used_mb = (stats.get("dataSize", 0) + stats.get("indexSize", 0)) / (1024 * 1024)
                self._last_checked = now
            except Exception as e:
                logger.warning(f"[DB-{self.index}] dbStats failed: {e}")
        return self._cached_used_mb

    async def is_full(self) -> bool:
        return await self.storage_used_mb() >= Telegram.DB_MAX_SIZE_MB

    async def file_count(self) -> int:
        return await self.file.count_documents({})


class MultiDatabase:
    """
    Storage layer that can span an UNLIMITED number of MongoDB clusters.

    - Shard 0 (the first DATABASE_URL) is the "primary" DB and always holds
      users/blacklist — these stay small, so there's no benefit sharding them.
    - The `file` collection (which grows without bound as people upload) is
      spread across every configured shard: the bot writes to whichever shard
      still has room and automatically rolls over to the next one once a
      shard crosses DB_MAX_SIZE_MB.
    - Every stored file id is returned as "{shard_index}:{ObjectId}" so a
      file can be fetched directly from the right shard without scanning
      every database.
    """

    def __init__(self):
        if not Telegram.DATABASE_URLS:
            raise ValueError(
                "No MongoDB configured. Set DATABASE_URL (and optionally "
                "DATABASE_URL_2, DATABASE_URL_3, ... for unlimited extra storage)."
            )
        self.shards = [
            _Shard(i, uri, Telegram.DATABASE_NAME)
            for i, uri in enumerate(Telegram.DATABASE_URLS)
        ]
        self.primary = self.shards[0]
        self._active_index = 0
        self._lock = asyncio.Lock()
        logger.info(f"MultiDatabase initialized with {len(self.shards)} MongoDB shard(s).")

    # ------------------------------------------------------------------ #
    # Shard selection / id encoding
    # ------------------------------------------------------------------ #
    async def _get_writable_shard(self) -> _Shard:
        async with self._lock:
            shard = self.shards[self._active_index]
            if await shard.is_full():
                for nxt in range(self._active_index + 1, len(self.shards)):
                    if not await self.shards[nxt].is_full():
                        logger.info(f"DB-{self._active_index + 1} full -> switching to DB-{nxt + 1}")
                        self._active_index = nxt
                        return self.shards[nxt]
                logger.warning(
                    "⚠️ All configured MongoDB shards are full! Add another "
                    "DATABASE_URL_N to your env to keep storing new files."
                )
                return shard
            return shard

    def _shard_for(self, index: int) -> _Shard:
        if index < 0 or index >= len(self.shards):
            raise FIleNotFound
        return self.shards[index]

    @staticmethod
    def _encode_id(shard_index: int, oid) -> str:
        return f"{shard_index}{SEP}{oid}"

    @staticmethod
    def _decode_id(compound_id: str):
        compound_id = str(compound_id)
        if SEP in compound_id:
            shard_str, oid = compound_id.split(SEP, 1)
            return int(shard_str), oid
        # backward-compat: links generated before multi-db support -> shard 0
        return 0, compound_id

    # ---------------------[ NEW USER ]---------------------#
    def new_user(self, id):
        return dict(id=id, join_date=time.time(), Links=0)

    # ---------------------[ ADD USER ]---------------------#
    async def add_user(self, id):
        await self.primary.col.insert_one(self.new_user(id))

    # ---------------------[ GET USER ]---------------------#
    async def get_user(self, id):
        return await self.primary.col.find_one({'id': int(id)})

    # ---------------------[ CHECK USER ]---------------------#
    async def total_users_count(self):
        return await self.primary.col.count_documents({})

    async def get_all_users(self):
        return self.primary.col.find({})

    # ---------------------[ REMOVE USER ]---------------------#
    async def delete_user(self, user_id):
        await self.primary.col.delete_many({'id': int(user_id)})

    # ---------------------[ BAN, UNBAN USER ]---------------------#
    def black_user(self, id):
        return dict(id=id, ban_date=time.time())

    async def ban_user(self, id):
        await self.primary.black.insert_one(self.black_user(id))

    async def unban_user(self, id):
        await self.primary.black.delete_one({'id': int(id)})

    async def is_user_banned(self, id):
        return bool(await self.primary.black.find_one({'id': int(id)}))

    async def total_banned_users_count(self):
        return await self.primary.black.count_documents({})

    # ---------------------[ ADD FILE TO DB ]---------------------#
    async def add_file(self, file_info):
        file_info["time"] = time.time()
        file_info.setdefault("linked", False)  # not yet attached to a Cinio movie/episode
        fetch_old = await self.get_file_by_fileuniqueid(file_info["user_id"], file_info["file_unique_id"])
        if fetch_old:
            return fetch_old["_id"]

        shard = await self._get_writable_shard()
        await self.count_links(file_info["user_id"], "+")
        result = await shard.file.insert_one(file_info)
        return self._encode_id(shard.index, result.inserted_id)

    # ---------------------[ FIND FILE IN DB ]---------------------#
    async def find_files(self, user_id, range):
        """A user's files can live on any shard, so results are merged across all of them."""
        all_docs = []
        for shard in self.shards:
            async for doc in shard.file.find({"user_id": user_id}):
                doc["_id"] = self._encode_id(shard.index, doc["_id"])
                all_docs.append(doc)
        all_docs.sort(key=lambda d: d.get("time", 0), reverse=True)
        total_files = len(all_docs)
        start, end = range[0] - 1, range[1]
        return iter(all_docs[start:end]), total_files

    async def get_file(self, _id):
        try:
            shard_index, raw_id = self._decode_id(_id)
            shard = self._shard_for(shard_index)
            file_info = await shard.file.find_one({"_id": ObjectId(raw_id)})
            if not file_info:
                raise FIleNotFound
            file_info["_id"] = self._encode_id(shard_index, file_info["_id"])
            return file_info
        except InvalidId:
            raise FIleNotFound

    async def get_file_by_fileuniqueid(self, id, file_unique_id, many=False):
        if many:
            async def _gen():
                for shard in self.shards:
                    async for doc in shard.file.find({"file_unique_id": file_unique_id}):
                        doc["_id"] = self._encode_id(shard.index, doc["_id"])
                        yield doc
            return _gen()
        for shard in self.shards:
            file_info = await shard.file.find_one({"user_id": id, "file_unique_id": file_unique_id})
            if file_info:
                file_info["_id"] = self._encode_id(shard.index, file_info["_id"])
                return file_info
        return False

    # ---------------------[ TOTAL FILES ]---------------------#
    async def total_files(self, id=None):
        total = 0
        for shard in self.shards:
            query = {"user_id": id} if id else {}
            total += await shard.file.count_documents(query)
        return total

    # ---------------------[ DELETE FILES ]---------------------#
    async def delete_one_file(self, _id):
        shard_index, raw_id = self._decode_id(_id)
        shard = self._shard_for(shard_index)
        await shard.file.delete_one({'_id': ObjectId(raw_id)})

    # ---------------------[ UPDATE FILES ]---------------------#
    async def update_file_ids(self, _id, file_ids: dict):
        shard_index, raw_id = self._decode_id(_id)
        shard = self._shard_for(shard_index)
        await shard.file.update_one({"_id": ObjectId(raw_id)}, {"$set": {"file_ids": file_ids}})

    async def count_links(self, id, operation: str):
        if operation == "-":
            await self.primary.col.update_one({"id": id}, {"$inc": {"Links": -1}})
        elif operation == "+":
            await self.primary.col.update_one({"id": id}, {"$inc": {"Links": 1}})

    # ---------------------[ CINIO ADMIN-PANEL INTEGRATION ]---------------------#
    async def get_unlinked_files(self, limit: int = 100):
        """Files uploaded to the bot that aren't attached to any movie/episode yet."""
        results = []
        for shard in self.shards:
            cursor = shard.file.find({"linked": {"$ne": True}}).sort("time", -1).limit(limit)
            async for doc in cursor:
                results.append(dict(
                    id=self._encode_id(shard.index, doc["_id"]),
                    file_name=doc.get("file_name", "Unknown"),
                    file_size=doc.get("file_size", 0),
                    mime_type=doc.get("mime_type", ""),
                    uploaded_at=doc.get("time", 0),
                ))
        results.sort(key=lambda d: d["uploaded_at"], reverse=True)
        return results[:limit]

    async def mark_file_linked(self, _id):
        shard_index, raw_id = self._decode_id(_id)
        shard = self._shard_for(shard_index)
        result = await shard.file.update_one(
            {"_id": ObjectId(raw_id)}, {"$set": {"linked": True}}
        )
        return result.matched_count > 0

    async def mark_file_unlinked(self, _id):
        """Called when a Content/Episode source using this file is deleted,
        so the file shows up again in the 'unlinked' picker."""
        shard_index, raw_id = self._decode_id(_id)
        shard = self._shard_for(shard_index)
        result = await shard.file.update_one(
            {"_id": ObjectId(raw_id)}, {"$set": {"linked": False}}
        )
        return result.matched_count > 0

    # ---------------------[ MULTI-DB STATS (for /status) ]---------------------#
    async def get_db_stats(self):
        rows = []
        total_used = 0.0
        total_files = 0
        for shard in self.shards:
            used = await shard.storage_used_mb(force=True)
            files = await shard.file_count()
            total_used += used
            total_files += files
            pct = min(used / Telegram.DB_MAX_SIZE_MB * 100, 100) if Telegram.DB_MAX_SIZE_MB else 0
            if pct >= 100:
                status = "🔴 Full"
            elif shard.index == self._active_index:
                status = "🟢 Active"
            else:
                status = "⚪ Standby"
            rows.append(dict(
                index=shard.index,
                used_mb=round(used, 1),
                max_mb=Telegram.DB_MAX_SIZE_MB,
                percent=round(pct, 1),
                files=files,
                status=status,
            ))
        return dict(
            shards=rows,
            total_shards=len(self.shards),
            active_shard=self._active_index,
            total_used_mb=round(total_used, 1),
            total_capacity_mb=Telegram.DB_MAX_SIZE_MB * len(self.shards),
            total_files=total_files,
        )


# Single shared instance for the whole bot. The original repo opened a brand
# new MongoClient in every file that used it (7x); we now connect once here
# and every module imports this same `db` object.
db = MultiDatabase()
