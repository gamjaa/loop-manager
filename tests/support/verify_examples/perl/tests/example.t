use strict;
use warnings;
use Test::More;
my $fixture = [2, 3];
is($fixture->[0] + $fixture->[1], 5, 'fixture sum');
SKIP: { skip 'example optional scenario', 1; }
{ local $TODO = 'example expected failure'; is(1, 2, 'TODO meaning'); }
subtest nested => sub { ok(1, 'nested assertion'); done_testing; };
done_testing;
