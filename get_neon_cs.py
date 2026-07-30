import base64
B64 = "cG9zdGdyZXNxbDovL25lb25kYl9vd25lcjpucGdfZ1BxZTRPRVJWUzlKQGVwLXNtYWxsLXBhcGVyLWF4aDYxeHIyLXBvb2xlci5jLTQudXMtZWFzdC0yLmF3cy5uZW9uLnRlY2gvbmVvbmRiP3NzbG1vZGU9cmVxdWlyZQ=="
def get_neon_cs():
    return base64.b64decode(B64).decode()
