import google.generativeai as genai

from app.core.config import settings

genai.configure(api_key=settings.GEMINI_API_KEY)

model_pro = genai.GenerativeModel("gemini-1.5-pro")
model_flash = genai.GenerativeModel("gemini-1.5-flash")
