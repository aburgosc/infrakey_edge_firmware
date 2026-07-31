# Cliente HTTP pequeño sobre SIM7080


class HttpClient:
    def __init__(self, modem, host, port=443, user_agent="sim7080mini/1.0", connect_host=None, open_timeout_ms=45000):
        self.modem = modem
        self.host = host
        self.port = port
        self.user_agent = user_agent
        self.connect_host = connect_host
        self.open_timeout_ms = open_timeout_ms

    def post_json(self, path, body, headers=None):
        return self.modem.http_post_json_return(
            host=self.host,
            connect_host=self.connect_host,
            port=self.port,
            user_agent=self.user_agent,
            path=path,
            body_dict=body,
            extra_headers=headers or {},
            open_timeout_ms=self.open_timeout_ms
        )

    def get_json(self, path, headers=None):
        return self.modem.http_get_json_return(
            host=self.host,
            connect_host=self.connect_host,
            port=self.port,
            user_agent=self.user_agent,
            path=path,
            extra_headers=headers or {},
            open_timeout_ms=self.open_timeout_ms
        )
