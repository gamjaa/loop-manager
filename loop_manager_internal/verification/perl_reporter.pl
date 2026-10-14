use strict;
use warnings;
use TAP::Parser;
use Test::More ();
use JSON::PP;
use Time::HiRes qw(time);
use FindBin;
use IO::Handle;
$| = 1;
die "unsupported Perl/TAP version\n" unless $^V eq v5.38.2 && $TAP::Parser::VERSION eq '3.44' && $Test::More::VERSION eq '1.302194';
my ($mode, $file, $spool, $identity_json) = @ARGV;
my %identity = $identity_json ? %{decode_json($identity_json)} : ();
if ($mode eq 'version') {
    print encode_json({perl => "$^V", parser => $TAP::Parser::VERSION, test_more => $Test::More::VERSION}), "\n";
    exit;
}
open my $raw, '>', $spool or die "TAP spool: $!";
$raw->autoflush(1);
my $parser = TAP::Parser->new({exec => [$^X, "-I$FindBin::Bin", '-MLoopManagerVerifyGuard', $file], spool => $raw});
my $start = time;
while (my $event = $parser->next) {
    my %record = (%identity, type => $event->type, raw => $event->raw, elapsed => time - $start);
    if ($event->is_test) {
        @record{qw(number description ok actual_ok skip todo)} =
            ($event->number, $event->description, $event->is_ok ? JSON::PP::true : JSON::PP::false,
             $event->is_actual_ok ? JSON::PP::true : JSON::PP::false,
             $event->has_skip ? JSON::PP::true : JSON::PP::false, $event->has_todo ? JSON::PP::true : JSON::PP::false);
    }
    print encode_json(\%record), "\n";
}
print encode_json({%identity, type => 'summary', planned => $parser->tests_planned, run => $parser->tests_run,
    skip_all => $parser->skip_all, errors => [$parser->parse_errors],
    good_plan => $parser->is_good_plan ? JSON::PP::true : JSON::PP::false,
    problems => $parser->has_problems ? JSON::PP::true : JSON::PP::false,
    exit => $parser->exit, wait => $parser->wait, elapsed => time - $start}), "\n";
