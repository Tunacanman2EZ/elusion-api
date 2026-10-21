"""shop - the vendors: buying from one, selling to one, and what one stocks (a
package since 18 Oct; the routes are as they were in app.py).

    POST /api/shop/buy          buy from a vendor: priced, paid and handed over
                                by the server, in one transaction
    POST /api/shop/sell         sell what is in one carried cell
    GET  /api/shop/<shop_id>    what a vendor stocks and pays, at the prices the
                                till will actually use

A SHOP ON THE CLIENT WOULD BE AN ITEM PRINTER WITH A RECEIPT. A purchase is two
claims - an item gained and gold spent - and a server that only checked what a
bag gained would be believing the other half. So the server checks the vendor
stocks the item, prices it from its OWN catalogue (the request carries no
price, and would not be believed if it did), takes the gold and grants the item
together, or does neither.

BUYING IS THE ECONOMY'S FIRST SINK. The gold is destroyed, not moved to a
vendor's pocket - through the ledger, reason shop_buy - so the supply report
sees it leave. GEAR IS ROLLED AT THE TILL, as a drop is: the shelf lists the
catalogue piece and its price, and what is handed over is rolled on the spot
(85-115% a stat, now and then a Perfect), at the same price whatever it rolls.
A Perfect bought is held from trade like one found.

SOME OF IT IS FREE (0.69.0). A shop's "free" list - the general store's is
the three mythic weapons - is handed over at no cost, one each per character
(counting its bag, what it wears and its account's bank), as the PLAIN
catalogue piece, never rolled. A drop always rolls, so the plain id is the
free one: gamedata.is_free() says so, and the sell route and every trade check
refuse it. That is why "free" is not a faucet.

SELLING IS A SMALL FAUCET, on purpose. The gold is minted, reason shop_sell, at
the vendor's sell_multiplier of an item's value, so an hour's junk is worth
less than the hour's coin (test_pacing.py measures it). A sale names one cell
and the item the game saw in it, like every carry route: a cell that no longer
holds it is the carry's 409, and nothing is sold.

Built like status/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. It writes no table of its own: the bag through carry/'s grant
and grid steps, the gold through app.py's ledger, the hold through trade/'s.
The catalogue - the shops, their stock, the prices, the roll - is gamedata,
handed in whole.
"""
from .routes import register  # noqa: F401
