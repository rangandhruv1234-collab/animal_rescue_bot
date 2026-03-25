import os
import urllib.request
from PIL import Image

def smartcrop_all_animals(image_path):
    try:
        # Try using YOLO if available
        from ultralytics import YOLO
        
        MODEL_PATH = "yolov8n.pt"
        if not os.path.exists(MODEL_PATH):
            urllib.request.urlretrieve(
                "https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov8n.pt",
                MODEL_PATH
            )
        
        model = YOLO(MODEL_PATH)
        results = model(image_path)
        animal_labels = ["dog", "cat", "horse", "cow", "sheep", "bird"]
        found_animals = []
        img = Image.open(image_path)
        width, height = img.size

        for result in results:
            for box in result.boxes:
                label = result.names[int(box.cls)]
                confidence = float(box.conf)
                if label in animal_labels:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    padding_x = (x2 - x1) * 0.1
                    padding_y = (y2 - y1) * 0.1
                    x1 = max(0, x1 - padding_x)
                    y1 = max(0, y1 - padding_y)
                    x2 = min(width, x2 + padding_x)
                    y2 = min(height, y2 + padding_y)
                    crop_path = f"animal_{len(found_animals)}.jpg"
                    cropped = img.crop((x1, y1, x2, y2))
                    cropped.save(crop_path)
                    found_animals.append({
                        "label": label,
                        "confidence": confidence,
                        "crop_path": crop_path
                    })
                    print(f"Found: {label} ({confidence:.0%}) -> {crop_path}")

        if found_animals:
            print(f"Total animals found: {len(found_animals)}")
            return found_animals

    except Exception as e:
        print(f"YOLO failed: {e}, falling back to full image")

    # Fallback: just use the full image if YOLO fails
    print("Using full image as fallback")
    img = Image.open(image_path)
    img.save("animal_0.jpg")
    return [{"label": "unknown", "confidence": 1.0, "crop_path": "animal_0.jpg"}]
