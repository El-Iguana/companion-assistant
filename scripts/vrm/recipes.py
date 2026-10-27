"""Custom expression recipes for VRoid faces: Fcl_ shape key suffix -> weight.

Names match Pantheon emotion labels so static/js/vtuber.mjs uses them directly.
"""
RECIPES = {
 "embarrassed": {"BRW_Sorrow":1.0, "EYE_Joy":.55, "MTH_Small":1.0, "MTH_Up":.3},
 "thoughtful":  {"BRW_Angry":.3, "BRW_Sorrow":.5, "EYE_Close":.25, "MTH_Small":.8, "MTH_Down":.4},
 "love":        {"BRW_Joy":.8, "EYE_Joy":.8, "MTH_Fun":1.0},
 "curiosity":   {"BRW_Surprised":.9, "EYE_Spread":.5, "MTH_Small":.6, "MTH_Up":.2},
 "confusion":   {"BRW_Sorrow":.8, "BRW_Angry":.4, "EYE_Close_R":.4, "MTH_Down":.5, "MTH_Small":.4},
 "fear":        {"BRW_Sorrow":1.0, "EYE_Surprised":.7, "EYE_Spread":.6, "MTH_Sorrow":.8, "MTH_Surprised":.3},
 "nervousness": {"BRW_Sorrow":.9, "EYE_Spread":.3, "MTH_Small":1.0, "MTH_Down":.5},
 "disgust":     {"BRW_Angry":.9, "EYE_Angry":.6, "EYE_Close_R":.35, "MTH_Angry":.8, "MTH_Down":.7},
 "annoyance":   {"BRW_Angry":.8, "EYE_Close":.35, "MTH_Down":.7, "MTH_Angry":.3},
 "pride":       {"BRW_Fun":.7, "EYE_Close":.35, "MTH_Up":1.0, "MTH_Fun":.5},
 "amusement":   {"BRW_Fun":1.0, "EYE_Fun":1.0, "MTH_Fun":1.0, "MTH_Up":.4},
 "excitement":  {"BRW_Surprised":.9, "EYE_Spread":.6, "MTH_Joy":1.0},
}
# Recipes that partly close the eyes: let blink blend down so it doesn't over-close.
BLINK_BLEND = {"embarrassed","thoughtful","love","annoyance","pride","amusement","confusion","disgust"}
