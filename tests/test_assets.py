

def test_an_avif_cover_photo_is_recognised_from_its_bytes():
    from verda.agency.assets import sniff
    assert sniff(b'\x00\x00\x00 ftypavif\x00\x00\x00\x00') == 'image/avif'
    assert sniff(b'\x00\x00\x00 ftypmp42') is None
