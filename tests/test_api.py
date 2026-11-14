import unittest

from figma_understanding.api import FigmaAPIError, FigmaBatchError, FigmaClient, JsonResponse, _NoRedirect


class APITests(unittest.TestCase):
    def test_inventory_endpoints_are_read_only_and_comments_request_markdown(self):
        requests = []
        def transport(request):
            requests.append(request)
            if request.full_url.endswith("/comments?as_md=true"):
                return JsonResponse(200, {}, {"comments": []})
            return JsonResponse(200, {}, {"document": {"type": "DOCUMENT"}})
        client = FigmaClient("secret", transport=transport)
        client.file("file-key")
        client.comments("file-key")
        self.assertEqual("GET", requests[0].get_method())
        self.assertEqual("GET", requests[1].get_method())
        self.assertIn("/v1/files/file-key?", requests[0].full_url)
        self.assertTrue(requests[1].full_url.endswith("/comments?as_md=true"))

    def test_batches_and_pins_version(self):
        requests = []
        def transport(request):
            requests.append(request)
            from urllib.parse import parse_qs, urlsplit
            ids = parse_qs(urlsplit(request.full_url).query)["ids"][0].split(",")
            return JsonResponse(200, {}, {"images": {node_id: f"https://images.figma.com/{node_id}.png" for node_id in ids}})
        client = FigmaClient("secret", transport=transport)
        result = client.render_urls("key", ["1:1", "2:2", "3:3"], version="v7", batch_size=2)
        self.assertEqual(2, len(requests))
        self.assertTrue(all("version=v7" in request.full_url for request in requests))
        from urllib.parse import parse_qs, urlsplit
        for request in requests:
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(["png"], query["format"])
            self.assertEqual(["1"], query["scale"])
        self.assertTrue(all(request.get_header("X-figma-token") == "secret" for request in requests))
        self.assertEqual(3, len(result))

    def test_null_and_missing_renders_remain_null(self):
        client = FigmaClient("secret", transport=lambda request: JsonResponse(200, {}, {"images": {"1:1": None}}))
        self.assertEqual({"1:1": None, "2:2": None}, client.render_urls("key", ["1:1", "2:2"], version=None))

    def test_429_is_bounded_and_uses_retry_after(self):
        calls = 0
        sleeps = []
        def transport(request):
            nonlocal calls
            calls += 1
            return JsonResponse(429, {"Retry-After": "2"}, {})
        client = FigmaClient("secret", transport=transport, sleep=sleeps.append)
        with self.assertRaises(FigmaAPIError) as caught:
            client.render_urls("key", ["1:1"], version=None)
        self.assertEqual(429, caught.exception.status)
        self.assertEqual(3, calls)
        self.assertEqual([2, 2], sleeps)

    def test_partial_batches_are_preserved_on_later_failure(self):
        calls = 0
        def transport(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return JsonResponse(200, {}, {"images": {"1:1": "https://images.figma.com/1.png"}})
            return JsonResponse(403, {}, {})
        client = FigmaClient("secret", transport=transport)
        with self.assertRaises(FigmaBatchError) as caught:
            client.render_urls("key", ["1:1", "2:2"], version="v1", batch_size=1)
        self.assertEqual({"1:1": "https://images.figma.com/1.png"}, caught.exception.partial)

    def test_partial_batches_survive_malformed_later_response(self):
        calls = 0
        def transport(request):
            nonlocal calls
            calls += 1
            return JsonResponse(200, {}, {"images": {"1:1": "https://images.figma.com/1.png"}}) if calls == 1 else JsonResponse(200, {}, {})
        with self.assertRaises(FigmaBatchError) as caught:
            FigmaClient("secret", transport=transport).render_urls("key", ["1:1", "2:2"], version=None, batch_size=1)
        self.assertEqual({"1:1": "https://images.figma.com/1.png"}, caught.exception.partial)

    def test_retry_after_header_is_case_insensitive_and_finite(self):
        responses = iter([JsonResponse(429, {"retry-after": "2"}, {}), JsonResponse(429, {"Retry-After": "nan"}, {}), JsonResponse(200, {}, {"images": {"1:1": None}})])
        sleeps = []
        client = FigmaClient("secret", transport=lambda request: next(responses), sleep=sleeps.append)
        client.render_urls("key", ["1:1"], version=None)
        self.assertEqual([2, 1], sleeps)

    def test_network_timeout_retries_are_bounded(self):
        calls = 0
        sleeps = []
        def transport(request):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TimeoutError("slow render")
            return JsonResponse(200, {}, {"images": {"1:1": None}})
        FigmaClient("secret", transport=transport, sleep=sleeps.append).render_urls("key", ["1:1"], version=None)
        self.assertEqual(3, calls)
        self.assertEqual([1, 2], sleeps)

    def test_batch_size_must_be_positive(self):
        with self.assertRaises(ValueError):
            FigmaClient("secret", transport=lambda request: JsonResponse(200, {}, {})).render_urls("key", ["1:1"], version=None, batch_size=0)

    def test_rejects_non_object_successful_json(self):
        client = FigmaClient(
            "secret",
            transport=lambda request: JsonResponse(200, {}, []),  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(FigmaAPIError, "non-object JSON"):
            client.file("key")

    def test_api_redirect_handler_does_not_forward_token(self):
        request = __import__("urllib.request").request.Request("https://api.figma.com/v1/files/key", headers={"X-Figma-Token": "secret"})
        self.assertIsNone(_NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://evil.example/steal"))
        client = FigmaClient("secret", transport=lambda request: JsonResponse(302, {"Location": "https://evil.example/steal"}, {}))
        with self.assertRaises(FigmaAPIError) as caught:
            client.file("key")
        self.assertEqual(302, caught.exception.status)

    def test_original_urls_uses_image_fills_endpoint(self):
        requests = []
        client = FigmaClient("secret", transport=lambda request: (requests.append(request) or JsonResponse(200, {}, {"images": {"ref": "https://images.figma.com/a.png"}})))
        self.assertEqual({"ref": "https://images.figma.com/a.png"}, client.original_urls("file-key"))
        self.assertIn("/v1/files/file-key/images?", requests[0].full_url)


if __name__ == "__main__":
    unittest.main()

