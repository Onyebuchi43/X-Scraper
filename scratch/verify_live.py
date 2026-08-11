import urllib.request

try:
    req = urllib.request.urlopen("http://157.230.181.59/", timeout=10)
    html = req.read().decode('utf-8')
    print("Length:", len(html))
    print("Contains Posting Mode:", "Posting Mode" in html or "c-posting-mode" in html)
    print("Contains Country Filter:", "Target Country" in html or "f-country" in html)
    print("Contains Image Upload:", "c-normal-media-file" in html)
except Exception as e:
    print("Error:", e)
