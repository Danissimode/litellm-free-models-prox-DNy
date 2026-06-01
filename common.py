import urllib.request


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            old_host = req.host
            new_host = new_req.host
            if old_host != new_host:
                for header in ["Authorization", "x-goog-api-key"]:
                    if header.title() in new_req.unredirected_hdrs:
                        del new_req.unredirected_hdrs[header.title()]
                    if header in new_req.unredirected_hdrs:
                        del new_req.unredirected_hdrs[header]
                    for k in list(new_req.headers.keys()):
                        if k.lower() == header.lower():
                            del new_req.headers[k]
        return new_req


_opener = urllib.request.build_opener(SafeRedirectHandler())
