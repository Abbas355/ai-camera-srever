"""Floor table occupancy — live zones + free/occupied history.

Modes: face (InsightFace) or yolo (YOLOv8n person/chair + YOLO-World desks).
Pipeline: detect → IoU tracking (ByteTrack-style IDs) → person→chair→table
association → temporal evidence → zone timers.
Isolated from PTZ Tables, Attendance, and Customers.
"""
