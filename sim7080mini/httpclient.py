# Cliente HTTP pequeño sobre SIM7080


class HttpClient:
    def __init__(self, modem, host, port=443, user_agent="sim7080mini/1.0"):
        self.modem = modem
        self.host = host
        self.port = port
        self.user_agent = user_agent

    def post_json(self, path, body, headers=None):
        return self.modem.http_post_json_return(
            host=self.host,
            port=self.port,
            user_agent=self.user_agent,
            path=path,
            body_dict=body,
            extra_headers=headers or {}
        )

    def get_json(self, path, headers=None):
        return self.modem.http_get_json_return(
            host=self.host,
            port=self.port,
            user_agent=self.user_agent,
            path=path,
            extra_headers=headers or {}
        )
