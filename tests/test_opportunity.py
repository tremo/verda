

def test_house_potential_follows_the_bag_evi_thresholds_not_building_claims():
    from verda.agency.opportunity import house_potential, tree_density
    assert house_potential('Tarla', 5738, 'imar izni alınabilir') == 0            # under 10 dönüm: no route, a claim in the text changes nothing
    assert house_potential('Tarla', 20000) == 80 and house_potential('Zeytinlik', 20000) == 40      # zeytinlik: 3573 applies, half
    assert house_potential('Bademlik', 11000) == 25                                # dikili would do from 10 dönüm, tree count unknown
    assert house_potential('Bademlik', 11000, '400 adet badem ağacı') == 50        # 400 / (27 x 11) is above the minimum density
    assert house_potential('Bademlik', 11000, '100 adet badem ağacı') == 0         # well under it: not dikili
    assert house_potential('Zeytinlik', 11000, '400 zeytin') == 25


def test_tree_density_reads_counts_and_ignores_text_without_them():
    from verda.agency.opportunity import tree_density
    assert round(tree_density('Taşınmaz içinde 70 adet zeytin ağacı, 10 adet badem ağacı vardır', 5738), 2) == 0.5
    assert tree_density('zeytinlik, manzaralı', 5000) is None and tree_density('70 zeytin', 0) is None
