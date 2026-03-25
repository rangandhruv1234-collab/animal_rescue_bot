from PIL import Image

def smartcrop_all_animals(image_path):
    print("Using full image — Gemini will analyze directly")
    img = Image.open(image_path)
    img.save("animal_0.jpg")
    return [{"label": "unknown", "confidence": 1.0, "crop_path": "animal_0.jpg"}]
