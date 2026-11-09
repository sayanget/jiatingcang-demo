# 加州家庭仓 demo (Uppcoder Spark)

Bilingual Chinese/English demo for a California home-warehouse workflow:

1. Seller inbound forecast (SKU-labeled inventory)
2. Seller uploads shipping label PDF(s) per order
3. Warehouse prints label, picks goods, packs, drops at USPS/UPS

Set `ADMIN_PASSWORD` in a local `.env` (not committed). `DATABASE_URL` is injected by Uppcoder.
