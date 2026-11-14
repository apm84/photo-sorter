"""Zero-shot prompt sets shared by all embedders."""

TYPE_PROMPTS: dict[str, list[str]] = {
    "photo": [
        "a photo taken with a camera", "a candid photo of people", "a photo of a landscape",
        "a snapshot of everyday life", "a family photo", "a photo of an animal", "a photo of food",
        "a portrait photo of a person", "a professional headshot photo",
    ],
    "screenshot": [
        "a screenshot of a phone screen", "a screenshot of a computer screen", "a screenshot of an app",
        "a screenshot of a text message conversation", "a screenshot of a website", "a screenshot of a social media post",
        "a screenshot of a video game",
    ],
    "meme": [
        "a meme with impact font caption", "an internet meme", "a funny meme with text on top and bottom",
        "a reaction image meme", "a joke image with a caption",
    ],
    "document": [
        "a photo of a receipt", "a scanned document", "a photo of a paper document with text",
        "a photo of a form", "a page of printed text", "a business card", "a photo of a whiteboard",
    ],
    "graphic": [
        "a digital illustration", "a cartoon drawing", "a logo", "a graphic design poster", "clip art",
        "a painting", "an infographic", "a flyer with text and graphics", "digital concept art", "a 3d render",
    ],
}

TAG_VOCAB = [
    "portrait", "party", "dog", "cat", "bird", "horse", "baby", "child", "group of people", "selfie", "wedding", "birthday cake",
    "christmas tree", "halloween costume", "beach", "ocean", "lake", "river", "mountains", "forest", "snow",
    "sunset", "city skyline", "street", "car", "truck", "boat", "airplane", "food", "restaurant", "church",
    "school", "sports", "football", "baseball", "basketball", "concert", "flowers", "garden", "farm",
    "tractor", "house", "kitchen", "living room", "graduation", "fireworks", "pool", "camping", "fishing",
    "hiking", "park", "zoo", "museum", "night", "text", "map", "receipt",
]



def tag_prompt(t: str) -> str:
    if t == "text":
        return "a photo with a lot of text"
    if t == "night":
        return "a photo taken at night"
    if t == "portrait":
        return "a portrait photo of a person"
    return f"a photo of {t}"
