from pymongo import MongoClient
from config import MONGO_URI

client = MongoClient(MONGO_URI)
db = client["auto_selling_bot"]

users_col = db["users"]
orders_col = db["orders"]
products_col = db["products"]
settings_col = db["settings"]
blocked_col = db["blocked_users"]

def init_settings():
    if not settings_col.find_one({"_id": "config"}):
        settings_col.insert_one({
            "_id": "config",
            "upi_id": "customupi@bank",
            "qr_code": None,
            "how_to_use_video": None,
            "number_buttons_position": "vertical",
            "video_buttons_position": "vertical"
        })

def get_setting(key):
    doc = settings_col.find_one({"_id": "config"})
    return doc.get(key) if doc else None

def update_setting(key, value):
    settings_col.update_one(
        {"_id": "config"},
        {"$set": {key: value}},
        upsert=True
    )
